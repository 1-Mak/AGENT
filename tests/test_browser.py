"""Источники с fetch: browser, strip_query и команда deadlines."""

import http.server
import sys
import threading
from datetime import date

import pytest

from sitewatch import cli
from sitewatch.browser import BrowserFetcher, BrowserUnavailable, LazyBrowser
from sitewatch.config import ConfigError, load_sources
from sitewatch.doctor import check_sources
from sitewatch.extract import extract_links
from sitewatch.fetch import FetchError
from sitewatch.pipeline import SourceError, discover, pick_fetcher, run
from sitewatch.store import Store
from tests.helpers import (
    INDEX_URL,
    FakeAnalyzer,
    FakeFetcher,
    Mailbox,
    index_html,
    item_html,
    item_url,
    make_settings,
    make_source,
    make_store,
)


def write_config(tmp_path, extra=""):
    path = tmp_path / "sources.yaml"
    path.write_text(f"sources:\n  - id: a\n    type: page\n    url: https://example.com/\n{extra}", encoding="utf-8")
    return path


# --- конфигурация ---


def test_fetch_defaults_to_http_and_accepts_browser(tmp_path):
    assert load_sources(write_config(tmp_path))[0].fetch == "http"
    assert load_sources(write_config(tmp_path, "    fetch: Browser\n"))[0].fetch == "browser"


def test_fetch_rejects_unknown_value_and_local_files(tmp_path):
    with pytest.raises(ConfigError, match="fetch должен быть"):
        load_sources(write_config(tmp_path, "    fetch: selenium\n"))
    path = tmp_path / "f.yaml"
    path.write_text("sources:\n  - {id: a, type: pdf, url: 'file:///C:/x.pdf', fetch: browser}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="локальных файлов"):
        load_sources(path)


def test_real_config_sources_load_and_new_ones_are_disabled_until_checked():
    sources = {s.id: s for s in load_sources("config/sources.yaml")}
    assert not sources["infostart-marking"].enabled and sources["infostart-marking"].strip_query
    assert sources["suz-announcement"].type == "pdf" and not sources["suz-announcement"].enabled


# --- strip_query ---

FORUM = (
    '<a href="/forum9/topic1/">Тема 1</a><a href="/forum9/topic1/?attempt=1">Тема 1 снова</a>'
    '<a href="/forum9/topic2/?attempt=1">Тема 2 (ссылка только с параметром)</a>'
)


def test_strip_query_merges_variants_and_keeps_topics_linked_only_with_params():
    pattern = r"/forum\d+/topic\d+/?$"
    assert extract_links(FORUM, "https://forum.example/", pattern) == ["https://forum.example/forum9/topic1/"]
    assert extract_links(FORUM, "https://forum.example/", pattern, strip_query=True) == [
        "https://forum.example/forum9/topic1/",
        "https://forum.example/forum9/topic2/",
    ]


def test_discover_passes_strip_query_from_source():
    source = make_source(url="https://forum.example/", link_pattern=r"/forum\d+/topic\d+/?$", strip_query=True)
    assert len(discover(source, FakeFetcher({"https://forum.example/": FORUM}))) == 2


# --- выбор загрузчика ---


def test_pick_fetcher_by_source():
    http, browser = FakeFetcher({}), FakeFetcher({})
    assert pick_fetcher(make_source(), http, browser) is http
    assert pick_fetcher(make_source(fetch="browser"), http, browser) is browser
    with pytest.raises(SourceError, match="fetch: browser"):
        pick_fetcher(make_source(fetch="browser"), http, None)


def test_run_reads_browser_sources_through_browser_only():
    pages = {INDEX_URL: index_html([20]), item_url(20): item_html(20)}
    http, browser, mailbox = FakeFetcher({}), FakeFetcher(pages), Mailbox()
    report = run(
        [make_source(fetch="browser")], fetcher=http, browser=browser, store=make_store(), analyzer=FakeAnalyzer(),
        mailer=mailbox, settings=make_settings(), today=date(2026, 10, 14),
    )
    assert http.calls == [] and browser.calls == [INDEX_URL, item_url(20)]
    assert len(report.changes) == 1 and len(mailbox.sent) == 1


def test_run_without_browser_counts_failure_instead_of_crashing():
    store = make_store()
    report = run(
        [make_source(fetch="browser")], fetcher=FakeFetcher({}), store=store, analyzer=FakeAnalyzer(), mailer=Mailbox(),
        settings=make_settings(), today=date(2026, 10, 14),
    )
    assert report.failed_sources == 1 and store.source_state("cz-releases").failures == 1


def test_check_sources_reports_missing_browser(capsys):
    rc = check_sources([make_source(fetch="browser")], FakeFetcher({}), "off", None)
    assert rc == 1 and "fetch: browser" in capsys.readouterr().out


# --- сам браузер ---


def test_missing_playwright_gives_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)  # как будто пакет не установлен
    with pytest.raises(BrowserUnavailable, match="pip install playwright"):
        BrowserFetcher("ua").get("http://127.0.0.1:1/")


def test_lazy_browser_does_not_start_until_used():
    lazy = LazyBrowser(user_agent="ua")
    lazy.close()  # закрытие неиспользованного браузера безопасно
    assert lazy._fetcher is None


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/spa":
            body = (
                "<html><body><div id=app></div><script>setTimeout(function(){"
                "document.getElementById('app').innerHTML='<a href=\"/forum9/topic7/\">Поломка API</a>'},50)"
                "</script></body></html>"
            ).encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
        elif self.path == "/file.pdf":
            self._send(200, "application/pdf", b"%PDF-1.4 test")
        else:
            self._send(404, "text/plain", b"no")

    def _send(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def local_site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def real_browser():
    pytest.importorskip("playwright.sync_api")
    fetcher = BrowserFetcher("sitewatch-test", timeout=15, delay=0, channel="chromium")
    try:
        fetcher._start()
    except BrowserUnavailable as e:
        pytest.skip(f"браузер недоступен: {e}")
    yield fetcher
    fetcher.close()


def test_real_browser_sees_links_drawn_by_script(real_browser, local_site):
    html = real_browser.get(local_site + "/spa")
    assert extract_links(html, local_site + "/spa", r"/forum\d+/topic\d+/?$") == [local_site + "/forum9/topic7/"]


def test_real_browser_reports_http_errors_and_downloads_files(real_browser, local_site):
    with pytest.raises(FetchError) as e:
        real_browser.get(local_site + "/missing")
    assert e.value.status == 404
    assert real_browser.get_bytes(local_site + "/file.pdf").startswith(b"%PDF")
    with pytest.raises(FetchError):
        real_browser.get_bytes(local_site + "/missing")


# --- команда deadlines ---


def deadlines_config(tmp_path):
    (tmp_path / "m.yaml").write_text(
        "milestones:\n  - {date: 20.10.2026, title: Поле expDate72, components: [Отчет]}\n", encoding="utf-8"
    )
    (tmp_path / "g.yaml").write_text("entries:\n  - {name: Отчет, where: документ «Отчет о нанесении»}\n", encoding="utf-8")
    path = tmp_path / "sources.yaml"
    path.write_text(
        "scope:\n  interests: мясо\n  glossary: g.yaml\n  milestones: m.yaml\n"
        "sources:\n  - {id: a, type: page, url: 'https://example.com/'}\n",
        encoding="utf-8",
    )
    return path


def run_cli(tmp_path, monkeypatch, *args):
    monkeypatch.setenv("SITEWATCH_DB", str(tmp_path / "state.db"))
    return cli.main(["--env", str(tmp_path / "нет.env"), "--log-file", "", *args])


@pytest.fixture(autouse=True)
def reset_logging():
    yield
    cli.shutdown_logging()


def test_deadlines_command_lists_milestones_with_where_and_supports_done(tmp_path, monkeypatch, capsys):
    config = deadlines_config(tmp_path)
    assert run_cli(tmp_path, monkeypatch, "deadlines", "--config", str(config)) == 0
    out = capsys.readouterr().out
    assert "20.10.2026" in out and "Поле expDate72" in out and "Отчет -> документ «Отчет о нанесении»" in out

    store = Store(str(tmp_path / "state.db"))
    key = store.load_deadlines()[0].key
    store.close()
    assert run_cli(tmp_path, monkeypatch, "deadlines", "--config", str(config), "--done", key[:4]) == 0
    assert "снят с календаря" in capsys.readouterr().out
    assert run_cli(tmp_path, monkeypatch, "deadlines", "--config", str(config)) == 0
    assert "В календаре нет сроков" in capsys.readouterr().out
    assert run_cli(tmp_path, monkeypatch, "deadlines", "--config", str(config), "--all") == 0
    assert "[снят]" in capsys.readouterr().out
    assert run_cli(tmp_path, monkeypatch, "deadlines", "--config", str(config), "--done", "zzzz") == 2


def test_deadlines_command_in_menu():
    from sitewatch.menu import ACTIONS

    assert ("12", "Календарь сроков: что и когда вступает в силу", ["deadlines"], True) in ACTIONS
