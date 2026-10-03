"""Отбор конференций по международным отношениям.

Два шага:
1. Быстрый фильтр по ключевым словам — бесплатно, отсеивает 95% ленты.
2. Нейросеть читает страницу кандидата: оценивает, насколько конференция
   действительно про МО (и про вашу тему, если она задана), и вытаскивает
   формат, оргвзнос, индексацию и тревожные признаки.

Если ключ нейросети не задан, бот работает только на шаге 1.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sources import Conference

# --- Шаг 1. Ключевые слова -------------------------------------------------

# Сильные признаки: почти наверняка про МО.
STRONG = [
    r"международн\w* отношени",
    r"внешн\w* политик", r"внешнеполитическ",
    r"миров\w* политик", r"миров\w* порядк", r"миропорядк", r"многополярн",
    r"дипломат", r"геополит", r"глобалистик", r"глобальн\w* управлени",
    r"регионоведени", r"востоковед", r"американист", r"европеист", r"африканист",
    r"международн\w* безопасност", r"международн\w* организаци",
    r"международн\w* прав", r"международн\w* конфликт", r"конфликтолог",
    r"мягк\w* сил", r"санкци", r"миротвор",
    r"\bООН\b", r"\bБРИКС\b", r"\bШОС\b", r"\bЕАЭС\b", r"\bСНГ\b", r"\bОДКБ\b", r"\bАСЕАН\b",
    r"international relations", r"foreign policy", r"diplomac", r"geopolitic", r"world order",
]
# Слабые признаки: сами по себе мало что значат.
WEAK = [
    r"интеграци", r"национальн\w* интерес", r"национальн\w* безопасност",
    r"глобализаци", r"глобальн\w* вызов", r"трансграничн", r"суверенитет",
    r"межгосударствен", r"зарубежн\w* стран", r"сотрудничеств\w* (?:стран|государств)",
    r"политолог", r"политическ\w* процесс",
]
WEAK_CATEGORIES = {"Политология", "Безопасность", "Государственное управление", "Общественные науки", "История"}

# «Международная научно-практическая конференция» — это про статус, а не про тему.
STATUS_PHRASE = re.compile(r"международн\w* (?:научн\w*|научно-\w+|студенческ\w*|молодёжн\w*|молодежн\w*|конференци|конкурс|форум|симпозиум|школ)", re.I)

_STRONG_RE = [re.compile(p, re.I) for p in STRONG]
_WEAK_RE = [re.compile(p, re.I) for p in WEAK]


def keyword_score(conf: Conference, extra_topic: str = "") -> tuple[int, list[str]]:
    text = STATUS_PHRASE.sub(" ", conf.title)
    hits: list[str] = []
    score = 0
    for rx in _STRONG_RE:
        m = rx.search(text)
        if m:
            score += 3
            hits.append(m.group(0))
    for rx in _WEAK_RE:
        m = rx.search(text)
        if m:
            score += 1
            hits.append(m.group(0))
    if WEAK_CATEGORIES & set(conf.categories):
        score += 1
    for word in re.findall(r"\w{5,}", extra_topic.lower()):
        if word[:-2] and word[:-2] in text.lower():
            score += 2
            hits.append(word)
    return score, hits


def is_candidate(conf: Conference, extra_topic: str = "") -> bool:
    score, _ = keyword_score(conf, extra_topic)
    return score >= 3


def mill_signals(conf: Conference) -> list[str]:
    """Признаки «сборника за оргвзнос». Это не приговор — повод проверить."""
    flags = []
    cats = set(conf.categories)
    if "Широкая тематика" in cats or len(conf.categories) >= 7:
        flags.append("очень широкая тематика")
    if conf.deadline and conf.start and conf.deadline >= conf.start:
        flags.append("заявки принимают до дня проведения")
    return flags


# --- Шаг 2. Нейросеть ------------------------------------------------------

PROMPT = """Ты помогаешь студенту-международнику выбирать научные конференции.
Ниже анонс конференции. Ответь ТОЛЬКО JSON-объектом без пояснений, с полями:
"relevance": целое 0-10 — насколько конференция подходит для доклада по международным отношениям{topic_clause}. 0 — совсем не про МО, 10 — профильная конференция по МО;
"why": одна короткая фраза по-русски, почему такая оценка;
"format": "очно", "онлайн", "очно и онлайн", "заочно (только публикация)" или "неизвестно";
"fee": "бесплатно", "платно: <сумма>" или "неизвестно";
"indexing": строка, например "РИНЦ" или "Scopus, РИНЦ", или "неизвестно";
"level": "студенческая", "молодых учёных", "всероссийская", "международная" или "неизвестно";
"organizer": кто проводит, коротко (вуз, институт РАН, коммерческий центр);
"red_flags": список коротких строк — признаки, что это платный сборник, а не настоящая конференция (широкая тематика, приём заявок до дня проведения, коммерческий организатор, публикация за деньги без рецензирования). Пустой список, если признаков нет.

Название: {title}
Даты: {dates}; заявки до: {deadline}; место: {place}
Тематики на сайте: {cats}

Текст страницы:
{text}
"""


@dataclass
class Analysis:
    relevance: int
    why: str = ""
    format: str = "неизвестно"
    fee: str = "неизвестно"
    indexing: str = "неизвестно"
    level: str = "неизвестно"
    organizer: str = ""
    red_flags: list[str] = field(default_factory=list)
    by_ai: bool = True

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "Analysis":
        return cls(**json.loads(s))


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("нейросеть не вернула JSON")
    return json.loads(m.group(0))


class Analyzer:
    def __init__(self, api_key: str | None, base_url: str | None, model: str | None):
        self.enabled = bool(api_key and model)
        self.model = model
        if self.enabled:
            from openai import AsyncOpenAI  # любой OpenAI-совместимый API
            self.client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)

    def without_ai(self, conf: Conference) -> Analysis:
        score, hits = keyword_score(conf)
        return Analysis(
            relevance=min(10, 4 + score),
            why="совпадение по словам: " + ", ".join(dict.fromkeys(hits)) if hits else "",
            red_flags=mill_signals(conf),
            by_ai=False,
        )

    async def analyze(self, conf: Conference, page_text: str, topic: str = "") -> Analysis:
        if not self.enabled:
            return self.without_ai(conf)
        topic_clause = f', и особенно для темы «{topic}»' if topic else ""
        prompt = PROMPT.format(
            topic_clause=topic_clause,
            title=conf.title,
            dates=f"{conf.start:%d.%m.%Y}–{conf.end:%d.%m.%Y}" if conf.start and conf.end else "неизвестно",
            deadline=f"{conf.deadline:%d.%m.%Y}" if conf.deadline else "неизвестно",
            place=conf.place or "неизвестно",
            cats=", ".join(conf.categories) or "нет",
            text=page_text or "(страницу не удалось загрузить — суди по названию)",
        )
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            data = _extract_json(resp.choices[0].message.content or "")
            flags = [str(f) for f in data.get("red_flags") or []]
            for f in mill_signals(conf):
                if f not in flags:
                    flags.append(f)
            return Analysis(
                relevance=int(data.get("relevance", 0)),
                why=str(data.get("why", "")),
                format=str(data.get("format", "неизвестно")),
                fee=str(data.get("fee", "неизвестно")),
                indexing=str(data.get("indexing", "неизвестно")),
                level=str(data.get("level", "неизвестно")),
                organizer=str(data.get("organizer", "")),
                red_flags=flags,
            )
        except Exception:  # сбой API не должен ронять бота
            return self.without_ai(conf)
