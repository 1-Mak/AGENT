"""Общие заготовки для тестов: макеты страниц и подмены сети/почты/агента."""

from __future__ import annotations

from pathlib import Path

from sitewatch.config import Settings
from sitewatch.fetch import FetchError
from sitewatch.models import Analysis, Change, DocRecord, Source
from sitewatch.store import Store

BASE = "https://xn--80ajghhoc2aj1c8b.xn--p1ai"
INDEX_URL = f"{BASE}/info/releasenotes/"
PATTERN = r"/info/releasenotes/chto-novogo-[^/?#]+/?$"


def slug(n: int) -> str:
    return f"chto-novogo-v-sisteme-s-{n:02d}-04-2026-po-{n + 4:02d}-04-2026"


def item_url(n: int) -> str:
    return f"{BASE}/info/releasenotes/{slug(n)}/"


def index_html(numbers: list[int]) -> str:
    links = "".join(f'<li><a href="/info/releasenotes/{slug(n)}/">Что нового {n}</a></li>' for n in numbers)
    return f"""<html><body>
    <nav><a href="/about/">О системе</a></nav>
    <main><ul>{links}</ul>
    <a href="/info/releasenotes/?PAGEN_1=2">Следующая страница</a>
    <a href="https://честныйзнак.рф/info/releasenotes/{slug(numbers[0])}/">Дубль с юникодным хостом</a>
    </main></body></html>"""


def item_html(n: int, body: str = "Добавлено новое требование к маркировке.") -> str:
    return f"""<html><head><title>Что нового {n} | Честный ЗНАК</title></head><body>
    <header>Меню сайта</header>
    <main><h1>Что нового в системе с {n:02d}.04.2026</h1>
    <p>{body}</p>
    <ul><li>Пункт один</li><li>Пункт два</li></ul>
    <a href="/upload/spec_{n}.pdf">Спецификация</a>
    <script>var counter = 12345;</script></main>
    <footer>© Честный ЗНАК</footer></body></html>"""


FIXTURES = Path(__file__).parent / "fixtures"


def pdf_bytes(name: str) -> bytes:
    return (FIXTURES / f"{name}.pdf").read_bytes()


class FakeFetcher:
    def __init__(self, pages: dict[str, object]):
        self.pages = pages
        self.calls: list[str] = []

    def _page(self, url: str):
        self.calls.append(url)
        if url not in self.pages:
            raise FetchError(f"{url}: HTTP 404", 404)  # как у настоящего сайта: страницы, которой нет в макете, нет и на сайте
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return value

    def get(self, url: str) -> str:
        return self._page(url)  # type: ignore[return-value]

    def get_bytes(self, url: str) -> bytes:
        value = self._page(url)
        assert isinstance(value, bytes), f"для {url} в макете ожидались байты (PDF)"
        return value


def make_analysis(importance: str = "высокая") -> Analysis:
    return Analysis(
        summary="Сроки сдвинуты.",
        affected_methods=["POST /cises/info"],
        deadlines=["до 01.07.2026 — переход"],
        required_actions=["обновить интеграцию"],
        importance=importance,
    )


def make_change(text="Текст документа", kind="new", diff="", title="Что нового <b>20</b>") -> Change:
    rec = DocRecord(item_url(20), "cz-releases", title, "h", text)
    return Change(make_source(), rec, kind, diff=diff)


class FakeAnalyzer:
    def __init__(self, importance: str = "средняя", disabled_reason: str | None = None, relevance: str = "прямо",
                 relevance_by_title: dict[str, str] | None = None, components: tuple[str, ...] = (), deadline: str = "",
                 methods: tuple[str, ...] = ()):
        self.importance = importance
        self.disabled_reason = disabled_reason
        self.relevance = relevance
        self.relevance_by_title = relevance_by_title or {}
        self.components = components
        self.deadline = deadline
        self.methods = methods
        self.seen: list[str] = []

    def analyze(self, change):
        self.seen.append(change.record.url)
        relevance = self.relevance_by_title.get(change.record.title, self.relevance)
        return Analysis(
            summary=f"Разбор: {change.record.title}",
            affected_methods=list(self.methods),
            deadlines=[],
            required_actions=[],
            importance=self.importance,
            relevance=relevance,
            relevance_note=f"отбор: {relevance}",
            nearest_deadline=self.deadline,
            affected_components=list(self.components),
        )


class Mailbox:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[tuple[str, str, str]] = []

    def __call__(self, subject: str, text: str, html_body: str) -> None:
        if self.fail:
            raise ConnectionError("SMTP недоступен")
        self.sent.append((subject, text, html_body))


def make_source(**overrides) -> Source:
    params = dict(
        id="cz-releases",
        name="Честный ЗНАК — релизы",
        type="list",
        url=INDEX_URL,
        focus="Требования, сроки, API.",
        link_pattern=PATTERN,
        recheck_latest=2,
        initial_notify=1,
    )
    params.update(overrides)
    return Source(**params)


def make_settings(**overrides) -> Settings:
    params = dict(mail_from="bot@example.com", mail_to=("me@example.com",), smtp_host="smtp.example.com", robots_mode="off")
    params.update(overrides)
    return Settings(**params)


def make_store() -> Store:
    return Store(":memory:")
