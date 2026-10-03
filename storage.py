"""Хранилище на SQLite: собранные анонсы, подписчики, что уже отправлено."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from sources import Conference

SCHEMA = """
CREATE TABLE IF NOT EXISTS conferences (
    link TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    start TEXT, finish TEXT, deadline TEXT,
    place TEXT, categories TEXT,
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id INTEGER PRIMARY KEY,
    topic TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS analyses (
    link TEXT NOT NULL, topic TEXT NOT NULL, data TEXT NOT NULL,
    PRIMARY KEY (link, topic)
);
CREATE TABLE IF NOT EXISTS sent (
    chat_id INTEGER NOT NULL, link TEXT NOT NULL, sent_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, link)
);
"""


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


class Storage:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    # --- анонсы ---
    def add_conferences(self, items: list[Conference]) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        added = 0
        for c in items:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO conferences VALUES (?,?,?,?,?,?,?,?)",
                (c.link, c.title,
                 c.start and c.start.isoformat(), c.end and c.end.isoformat(),
                 c.deadline and c.deadline.isoformat(),
                 c.place, "|".join(c.categories), now),
            )
            added += cur.rowcount
        self.db.commit()
        return added

    def open_conferences(self, seen_within_days: int, today: date | None = None) -> list[Conference]:
        """Анонсы, собранные за последние N дней, по которым ещё можно подать заявку."""
        today = today or date.today()
        since = (datetime.now() - timedelta(days=seen_within_days)).isoformat(timespec="seconds")
        rows = self.db.execute(
            "SELECT link,title,start,finish,deadline,place,categories FROM conferences "
            "WHERE first_seen >= ? AND (deadline IS NULL OR deadline >= ?)",
            (since, today.isoformat()),
        ).fetchall()
        return [
            Conference(link=r[0], title=r[1], start=_d(r[2]), end=_d(r[3]), deadline=_d(r[4]),
                       place=r[5] or "", categories=[x for x in (r[6] or "").split("|") if x])
            for r in rows
        ]

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM conferences").fetchone()[0]

    # --- подписчики ---
    def subscribe(self, chat_id: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO subscribers(chat_id) VALUES (?)", (chat_id,))
        self.db.commit()

    def unsubscribe(self, chat_id: int) -> None:
        self.db.execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))
        self.db.commit()

    def subscribers(self) -> list[tuple[int, str]]:
        return self.db.execute("SELECT chat_id, topic FROM subscribers").fetchall()

    def set_topic(self, chat_id: int, topic: str) -> None:
        self.subscribe(chat_id)
        self.db.execute("UPDATE subscribers SET topic=? WHERE chat_id=?", (topic, chat_id))
        self.db.commit()

    def topic(self, chat_id: int) -> str:
        row = self.db.execute("SELECT topic FROM subscribers WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row else ""

    # --- кэш оценок нейросети ---
    def get_analysis(self, link: str, topic: str) -> str | None:
        row = self.db.execute("SELECT data FROM analyses WHERE link=? AND topic=?", (link, topic)).fetchone()
        return row[0] if row else None

    def save_analysis(self, link: str, topic: str, data: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO analyses VALUES (?,?,?)", (link, topic, data))
        self.db.commit()

    # --- что уже отправляли ---
    def already_sent(self, chat_id: int) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT link FROM sent WHERE chat_id=?", (chat_id,))}

    def mark_sent(self, chat_id: int, links: list[str]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        self.db.executemany("INSERT OR IGNORE INTO sent VALUES (?,?,?)", [(chat_id, l, now) for l in links])
        self.db.commit()
