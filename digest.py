"""Сборка еженедельной сводки."""

from __future__ import annotations

from datetime import date
from html import escape

from analyzer import Analysis, Analyzer, is_candidate
from sources import Conference, fetch_details
from storage import Storage

MAX_MESSAGE = 3800  # лимит Telegram 4096 символов, оставляем запас


def _ru_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def _dates(c: Conference) -> str:
    if not c.start:
        return "даты не указаны"
    if not c.end or c.end == c.start:
        return f"{c.start:%d.%m.%Y}"
    return f"{c.start:%d.%m}–{c.end:%d.%m.%Y}"


def format_item(n: int, c: Conference, a: Analysis, today: date) -> str:
    lines = [f'<b>{n}. <a href="{escape(c.link)}">{escape(c.title)}</a></b>']
    lines.append(f"📅 {_dates(c)} · 📍 {escape(c.place or 'место не указано')}")
    if c.deadline:
        left = (c.deadline - today).days
        tail = "сегодня последний день" if left == 0 else f"осталось {_ru_days(left)}"
        hot = "⏰ " if left <= 7 else "📝 "
        lines.append(f"{hot}Заявки до {c.deadline:%d.%m} ({tail})")
    score = f"🎯 {a.relevance}/10" if a.by_ai else "🎯"
    if a.why:
        score += f" — {escape(a.why)}"
    lines.append(score)
    if a.by_ai:
        facts = [f"💰 {a.fee}", f"🖥 {a.format}", f"📚 {a.indexing}"]
        lines.append(escape(" · ".join(f for f in facts if "неизвестно" not in f)) or "")
        if a.organizer:
            lines.append(f"🏛 {escape(a.organizer)}")
    if a.red_flags:
        lines.append("⚠️ Проверьте: " + escape("; ".join(a.red_flags)))
    return "\n".join(l for l in lines if l)


def pack_messages(header: str, items: list[str], footer: str = "") -> list[str]:
    messages, current = [], header
    for item in items:
        if len(current) + len(item) + 2 > MAX_MESSAGE:
            messages.append(current)
            current = ""
        current += ("\n\n" if current else "") + item
    if footer:
        current += "\n\n" + footer
    messages.append(current)
    return messages


async def build_digest(
    storage: Storage,
    analyzer: Analyzer,
    chat_id: int,
    topic: str,
    days: int = 7,
    min_relevance: int = 6,
    only_new: bool = True,
    details_loader=fetch_details,
    today: date | None = None,
) -> tuple[list[str], list[str]]:
    """Возвращает (сообщения, ссылки вошедших конференций)."""
    today = today or date.today()
    pool = storage.open_conferences(days, today)
    sent = storage.already_sent(chat_id) if only_new else set()
    candidates = [c for c in pool if c.link not in sent and is_candidate(c, topic)]

    # Нейросеть смотрим только для тех, кого ещё не оценивали с этой темой.
    need_pages = [c for c in candidates if storage.get_analysis(c.link, topic) is None]
    pages = await details_loader(need_pages) if (need_pages and analyzer.enabled) else {}

    rated: list[tuple[Conference, Analysis]] = []
    for c in candidates:
        cached = storage.get_analysis(c.link, topic)
        if cached:
            a = Analysis.from_json(cached)
        else:
            a = await analyzer.analyze(c, pages.get(c.link, ""), topic)
            if a.by_ai:
                storage.save_analysis(c.link, topic, a.to_json())
        if a.relevance >= min_relevance:
            rated.append((c, a))

    rated.sort(key=lambda x: (-x[1].relevance, x[0].deadline or date.max))
    topic_line = f"\nТема: <i>{escape(topic)}</i>" if topic else ""
    header = (
        "🌍 <b>Конференции по международным отношениям</b>"
        f"{topic_line}\nПросмотрено анонсов за {_ru_days(days)}: {len(pool)}, подошло: {len(rated)}"
    )
    if not rated:
        return [header + "\n\nНа этой неделе подходящих конференций не нашлось. Продолжаю искать."], []

    items = [format_item(i + 1, c, a, today) for i, (c, a) in enumerate(rated)]
    footer = "" if analyzer.enabled else "<i>Отбор по ключевым словам: ключ нейросети не задан.</i>"
    return pack_messages(header, items, footer), [c.link for c, _ in rated]
