"""Загрузка анонсов конференций с konferencii.ru.

Источник — официальная RSS-лента сайта (её сайт сам предлагает для подписки).
Страницы отдельных конференций (/info/...) разрешены в robots.txt, но мы
открываем их только для отобранных кандидатов и с паузой между запросами,
как просит сайт (Crawl-delay: 10).
"""

from __future__ import annotations

import asyncio
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

USER_AGENT = "IR-ConferenceDigestBot/1.0 (personal Telegram bot; weekly digest)"
CRAWL_DELAY_SECONDS = 10

DATE_RE = r"(\d{2}\.\d{2}\.\d{4})"
DESC_RE = re.compile(
    DATE_RE + r"\s*[—–-]\s*" + DATE_RE      # даты проведения
    + r"\s*\(заявки до\s*" + DATE_RE + r"\)"  # срок заявок
    + r",?\s*(?P<place>[^<(]*)"               # страна, город
    + r"(?:<br\s*/?>)?\s*(?:\((?P<cats>[^)]*)\))?",
    re.S,
)


@dataclass
class Conference:
    link: str
    title: str
    start: date | None = None
    end: date | None = None
    deadline: date | None = None
    place: str = ""
    categories: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.link.rstrip("/").rsplit("/", 1)[-1]


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d.%m.%Y").date()
    except ValueError:
        return None


def _split_categories(raw: str) -> list[str]:
    # Сайт перечисляет тематики через запятую, но в некоторых названиях
    # тематик тоже есть запятые («Экономика, Управление, Финансы»),
    # поэтому возвращаем «как есть» по частям — для фильтра этого достаточно.
    return [c.strip() for c in raw.split(",") if c.strip()]


def parse_rss(xml_text: str) -> list[Conference]:
    root = ET.fromstring(xml_text)
    result: list[Conference] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = html.unescape(item.findtext("description") or "")
        if not title or not link:
            continue
        conf = Conference(link=link, title=title)
        m = DESC_RE.search(desc)
        if m:
            conf.start = _parse_date(m.group(1))
            conf.end = _parse_date(m.group(2))
            conf.deadline = _parse_date(m.group(3))
            conf.place = (m.group("place") or "").strip().rstrip(",")
            conf.categories = _split_categories(m.group("cats") or "")
        result.append(conf)
    return result


async def fetch_feeds(urls: list[str]) -> list[Conference]:
    found: dict[str, Conference] = {}
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        for url in urls:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
            for conf in parse_rss(resp.text):
                found[conf.link] = conf
    return list(found.values())


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)


def html_to_text(page: str) -> str:
    page = _SCRIPT_RE.sub(" ", page)
    page = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", page, flags=re.I)
    text = html.unescape(_TAG_RE.sub(" ", page))
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def extract_main_text(page_text: str, title: str, limit: int = 6000) -> str:
    """Отрезаем меню и рекламу: начинаем с названия конференции."""
    key = title[:40]
    pos = page_text.find(key)
    if pos == -1:
        pos = 0
    return page_text[pos:pos + limit]


async def fetch_details(conferences: list[Conference]) -> dict[str, str]:
    """Тексты страниц конференций, с паузой между запросами."""
    texts: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        for i, conf in enumerate(conferences):
            if i:
                await asyncio.sleep(CRAWL_DELAY_SECONDS)
            try:
                resp = await client.get(conf.link, follow_redirects=True)
                resp.raise_for_status()
                texts[conf.link] = extract_main_text(html_to_text(resp.text), conf.title)
            except httpx.HTTPError:
                texts[conf.link] = ""
    return texts
