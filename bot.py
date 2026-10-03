"""Telegram-бот: раз в неделю присылает конференции по международным отношениям.

Запуск:            python bot.py
Проверка без Telegram на сохранённой ленте:
                   python bot.py --demo tests/sample_rss.xml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
from datetime import date, time
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from analyzer import Analyzer
from digest import build_digest
from sources import fetch_feeds, parse_rss
from storage import Storage

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("conf_bot")

WEEKDAYS = {"вс": 0, "пн": 1, "вт": 2, "ср": 3, "чт": 4, "пт": 5, "сб": 6,
            "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
RSS_URLS = [u.strip() for u in os.getenv("RSS_URLS", "https://konferencii.ru/rss").split(",") if u.strip()]
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
DIGEST_DAY = WEEKDAYS.get(os.getenv("DIGEST_DAY", "пн").strip().lower(), 1)
DIGEST_TIME = time.fromisoformat(os.getenv("DIGEST_TIME", "10:00")).replace(tzinfo=TZ)
COLLECT_EVERY_HOURS = float(os.getenv("COLLECT_EVERY_HOURS", "4"))
MIN_RELEVANCE = int(os.getenv("MIN_RELEVANCE", "6"))
DB_PATH = os.getenv("DB_PATH", "conferences.db")
ALLOWED = {int(x) for x in re.findall(r"-?\d+", os.getenv("ALLOWED_CHAT_IDS", ""))}

storage = Storage(DB_PATH)
analyzer = Analyzer(os.getenv("LLM_API_KEY"), os.getenv("LLM_BASE_URL"), os.getenv("LLM_MODEL"))

HELP = (
    "Я ищу научные конференции по международным отношениям на konferencii.ru "
    "и раз в неделю присылаю короткую сводку: что за конференция, до какого числа заявки, "
    "бесплатно ли участие, есть ли РИНЦ, и предупреждаю о сомнительных «сборниках за оргвзнос».\n\n"
    "Команды:\n"
    "/digest — сводка прямо сейчас\n"
    "/topic <тема> — уточнить тему, например: /topic российско-китайские отношения\n"
    "/topic — показать текущую тему, /topic - — сбросить\n"
    "/status — сколько анонсов собрано\n"
    "/stop — отписаться"
)


# --- сбор и рассылка -------------------------------------------------------

async def collect(context=None) -> int:
    try:
        items = await fetch_feeds(RSS_URLS)
        added = storage.add_conferences(items)
        log.info("Лента: получено %d, новых %d, всего в базе %d", len(items), added, storage.count())
        return added
    except Exception:
        log.exception("Не удалось загрузить ленту")
        return 0


async def send_digest(bot, chat_id: int, topic: str, only_new: bool) -> None:
    messages, links = await build_digest(
        storage, analyzer, chat_id, topic,
        days=10 if only_new else 21, min_relevance=MIN_RELEVANCE, only_new=only_new,
    )
    for text in messages:
        await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
    if only_new:
        storage.mark_sent(chat_id, links)


async def weekly_job(context) -> None:
    await collect()
    for chat_id, topic in storage.subscribers():
        try:
            await send_digest(context.bot, chat_id, topic, only_new=True)
        except Exception:
            log.exception("Не удалось отправить сводку в чат %s", chat_id)


# --- команды ----------------------------------------------------------------

def allowed(update) -> bool:
    return not ALLOWED or update.effective_chat.id in ALLOWED


async def cmd_start(update, context) -> None:
    if not allowed(update):
        await update.message.reply_text(f"Это личный бот. Ваш chat id: {update.effective_chat.id}")
        return
    storage.subscribe(update.effective_chat.id)
    await update.message.reply_text("Подписал вас на еженедельную сводку.\n\n" + HELP)


async def cmd_help(update, context) -> None:
    await update.message.reply_text(HELP)


async def cmd_stop(update, context) -> None:
    storage.unsubscribe(update.effective_chat.id)
    await update.message.reply_text("Отписал. Вернуться — /start")


async def cmd_topic(update, context) -> None:
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    text = " ".join(context.args).strip()
    if not text:
        current = storage.topic(chat_id) or "не задана (ищу по всем международным отношениям)"
        await update.message.reply_text(f"Текущая тема: {current}")
        return
    if text == "-":
        storage.set_topic(chat_id, "")
        await update.message.reply_text("Тема сброшена: ищу по всем международным отношениям.")
        return
    storage.set_topic(chat_id, text[:200])
    await update.message.reply_text(f"Запомнил тему: {text[:200]}\nПроверить сразу — /digest")


async def cmd_digest(update, context) -> None:
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    await update.message.reply_text("Ищу, это займёт до пары минут…")
    if storage.count() == 0:
        await collect()
    await send_digest(context.bot, chat_id, storage.topic(chat_id), only_new=False)


async def cmd_status(update, context) -> None:
    days = ["воскресенье", "понедельник", "вторник", "среду", "четверг", "пятницу", "субботу"]
    ai = f"нейросеть: {analyzer.model}" if analyzer.enabled else "нейросеть не подключена, отбор по словам"
    await update.message.reply_text(
        f"Анонсов в базе: {storage.count()}\n"
        f"Сводка: каждую {days[DIGEST_DAY]} в {DIGEST_TIME:%H:%M} ({TZ.key})\n{ai}"
    )


def run_bot() -> None:
    from telegram.ext import Application, CommandHandler

    if not TOKEN:
        raise SystemExit("Не задан TELEGRAM_TOKEN — см. README, шаг 1.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("topic", cmd_topic))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("status", cmd_status))

    jq = app.job_queue
    jq.run_repeating(collect, interval=COLLECT_EVERY_HOURS * 3600, first=5, name="collect")
    jq.run_daily(weekly_job, time=DIGEST_TIME, days=(DIGEST_DAY,), name="weekly")
    log.info("Бот запущен. %s", "Нейросеть: " + analyzer.model if analyzer.enabled else "Без нейросети.")
    app.run_polling()


# --- демо-режим без Telegram -------------------------------------------------

async def demo(rss_path: str, topic: str, online: bool, today: date) -> None:
    import tempfile
    from html import unescape
    import digest as digest_mod
    import storage as storage_mod

    with open(rss_path, encoding="utf-8") as f:
        items = parse_rss(f.read())
    db = storage_mod.Storage(os.path.join(tempfile.mkdtemp(), "demo.db"))
    db.add_conferences(items)

    async def no_pages(confs):
        return {}

    loader = None if online else no_pages
    kwargs = {"details_loader": loader} if loader else {}
    messages, _ = await digest_mod.build_digest(
        db, analyzer, chat_id=0, topic=topic, days=7, min_relevance=MIN_RELEVANCE,
        today=today, **kwargs,
    )
    for m in messages:
        m = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2\n   \1", m)
        print(unescape(re.sub(r"</?[bi]>", "", m)))
        print("-" * 60)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--demo", metavar="RSS_FILE", help="показать сводку по сохранённой ленте без Telegram")
    p.add_argument("--topic", default="", help="тема для демо-режима")
    p.add_argument("--online", action="store_true", help="в демо загружать страницы конференций")
    p.add_argument("--today", default="", help="дата «сегодня» для демо, ГГГГ-ММ-ДД")
    args = p.parse_args()
    if args.demo:
        today = date.fromisoformat(args.today) if args.today else date.today()
        asyncio.run(demo(args.demo, args.topic, args.online, today))
    else:
        run_bot()
