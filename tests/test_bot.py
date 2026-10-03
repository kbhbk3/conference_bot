"""Проверки без сети: разбор ленты, фильтр, сводка с подменённой нейросетью."""
import asyncio, os, sys, tempfile, types
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyzer import Analyzer, is_candidate
from digest import build_digest
from sources import parse_rss
from storage import Storage

HERE = os.path.dirname(os.path.abspath(__file__))
TODAY = date(2026, 10, 2)


def load():
    with open(os.path.join(HERE, "sample_rss.xml"), encoding="utf-8") as f:
        return parse_rss(f.read())


def test_parse():
    items = load()
    assert len(items) == 15
    first = items[0]
    assert first.start == date(2026, 11, 26) and first.deadline == date(2026, 11, 22)
    assert first.place == "Россия, Москва" and first.categories == ["Транспортные коммуникации"]


def test_filter():
    picked = {c.title for c in load() if is_candidate(c)}
    assert any("мировом порядке" in t for t in picked)
    assert any("Геополитика" in t for t in picked)
    assert not any("Педагогика высшей школы" in t for t in picked)   # «международный» — статус, не тема
    assert not any("Сахарный диабет" in t for t in picked)


class FakeCompletions:
    async def create(self, model, messages, temperature):
        prompt = messages[0]["content"]
        rel = 9 if "дипломатия" in prompt else 3
        content = ('Вот оценка: {"relevance": %d, "why": "тест", "format": "очно и онлайн", '
                   '"fee": "бесплатно", "indexing": "РИНЦ", "level": "студенческая", '
                   '"organizer": "вуз", "red_flags": []}' % rel)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))])


def test_digest_with_fake_ai():
    db = Storage(os.path.join(tempfile.mkdtemp(), "t.db"))
    db.add_conferences(load())
    an = Analyzer(None, None, None)
    an.enabled, an.model = True, "fake"
    an.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))

    async def pages(confs):
        return {c.link: "текст" for c in confs}

    msgs, links = asyncio.run(build_digest(db, an, 1, "", details_loader=pages, today=TODAY))
    text = "\n".join(msgs)
    assert links == ["https://example.invalid/test-1"], links          # геополитика получила 3/10 и отсеяна
    assert "9/10" in text and "бесплатно" in text and "РИНЦ" in text
    db.mark_sent(1, links)
    msgs2, links2 = asyncio.run(build_digest(db, an, 1, "", details_loader=pages, today=TODAY))
    assert links2 == [] and "не нашлось" in msgs2[0]                   # повторно не присылает


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("OK", name)
