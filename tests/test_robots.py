"""robots.txt: что разрешено, как читать отказы сайта, три режима и поведение в запуске."""

import logging

import pytest

from sitewatch.doctor import check_sources
from sitewatch.fetch import FetchError
from sitewatch.pipeline import run
from sitewatch.robots import GuardedFetcher, RobotsBlocked, RobotsPolicy
from tests.helpers import (
    BASE,
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

ROBOTS_URL = f"{BASE}/robots.txt"
SITE = "https://site.example"


def policy_for(robots_text=None, error=None, host=SITE):
    pages = {f"{host}/robots.txt": error if error else robots_text}
    fetcher = FakeFetcher(pages)
    return RobotsPolicy(fetcher), fetcher


# --- разбор robots.txt ---


def test_rules_for_everyone_and_crawl_delay():
    policy, _ = policy_for("User-agent: *\nDisallow: /private/\nCrawl-delay: 5\n")
    assert policy.check(f"{SITE}/public/page").allowed
    blocked = policy.check(f"{SITE}/private/doc")
    assert not blocked.allowed and blocked.crawl_delay == 5 and blocked.robots_url == f"{SITE}/robots.txt"


def test_disallow_everything_and_agent_specific_rules():
    assert not policy_for("User-agent: *\nDisallow: /\n")[0].check(f"{SITE}/x").allowed
    assert not policy_for("User-agent: sitewatch\nDisallow: /\n")[0].check(f"{SITE}/x").allowed  # правило адресовано нам
    assert policy_for("User-agent: googlebot\nDisallow: /\n")[0].check(f"{SITE}/x").allowed  # чужим роботам — не нам


def test_missing_or_broken_robots_does_not_block():
    missing, _ = policy_for(error=FetchError("HTTP 404", 404))
    verdict = missing.check(f"{SITE}/x")
    assert verdict.allowed and "нет" in verdict.note  # 4xx по стандарту = файла нет

    down, _ = policy_for(error=FetchError("HTTP 503", 503))
    assert down.check(f"{SITE}/x").allowed and "недоступен" in down.check(f"{SITE}/x").note

    timeout, _ = policy_for(error=FetchError("ConnectionError: нет сети", None))
    assert timeout.check(f"{SITE}/x").allowed

    soft404, _ = policy_for("<html><body>Страница не найдена</body></html>")  # сайт отдал HTML вместо robots.txt
    assert soft404.check(f"{SITE}/x").allowed


def test_robots_is_read_once_per_host_and_local_files_are_skipped():
    policy, fetcher = policy_for("User-agent: *\nDisallow: /private/\n")
    for path in ("/a", "/b", "/private/c"):
        policy.check(f"{SITE}{path}")
    assert fetcher.calls == [f"{SITE}/robots.txt"]
    assert policy.check("file:///tmp/spec.json").allowed and len(fetcher.calls) == 1


# --- обёртка загрузчика ---


def blocked_guard(mode):
    fetcher = FakeFetcher({f"{SITE}/robots.txt": "User-agent: *\nDisallow: /private/\n", f"{SITE}/private/doc": "секрет", f"{SITE}/open": "ok"})
    return GuardedFetcher(fetcher, RobotsPolicy(fetcher), mode), fetcher


def test_respect_mode_refuses_with_a_clear_way_out():
    guard, fetcher = blocked_guard("respect")
    assert guard.get(f"{SITE}/open") == "ok"
    with pytest.raises(RobotsBlocked, match="robots: warn") as info:
        guard.get(f"{SITE}/private/doc")
    assert "SITEWATCH_ROBOTS=warn" in str(info.value)
    assert f"{SITE}/private/doc" not in fetcher.calls  # запрещённый адрес даже не запрашивался


def test_warn_mode_loads_but_leaves_a_trace(caplog):
    guard, _ = blocked_guard("warn")
    with caplog.at_level(logging.WARNING):
        assert guard.get(f"{SITE}/private/doc") == "секрет"
    assert "запрещает" in caplog.text and "режим warn" in caplog.text


def test_guard_also_covers_binary_downloads():
    fetcher = FakeFetcher({f"{SITE}/robots.txt": "User-agent: *\nDisallow: /files/\n", f"{SITE}/files/a.pdf": b"%PDF-1.4"})
    guard = GuardedFetcher(fetcher, RobotsPolicy(fetcher), "respect")
    with pytest.raises(RobotsBlocked):
        guard.get_bytes(f"{SITE}/files/a.pdf")


# --- в запуске ---


def run_with(settings, source, robots_text):
    pages = {ROBOTS_URL: robots_text, INDEX_URL: index_html([20]), item_url(20): item_html(20)}
    store, mailbox = make_store(), Mailbox()
    fetcher = FakeFetcher(pages)
    report = run([source], fetcher=fetcher, store=store, analyzer=FakeAnalyzer(), mailer=mailbox, settings=settings)
    return report, store, mailbox, fetcher


DENY_INFO = "User-agent: *\nDisallow: /info/\n"


def test_respect_mode_turns_a_forbidden_source_into_a_failure_with_explanation():
    report, store, mailbox, fetcher = run_with(make_settings(robots_mode="respect"), make_source(), DENY_INFO)
    assert report.failed_sources == 1 and report.changes == [] and mailbox.sent == []
    assert INDEX_URL not in fetcher.calls  # страницу не запрашивали
    assert "robots.txt сайта запрещает" in store.conn.execute("SELECT last_error FROM sources").fetchone()[0]


def test_warn_mode_and_per_source_off_both_let_the_run_through():
    warn, *_ = run_with(make_settings(robots_mode="warn"), make_source(), DENY_INFO)
    assert warn.failed_sources == 0 and len(warn.changes) == 1

    off_for_source, _, _, fetcher = run_with(make_settings(robots_mode="respect"), make_source(robots="off"), DENY_INFO)
    assert off_for_source.failed_sources == 0 and ROBOTS_URL not in fetcher.calls  # robots.txt даже не читали


def test_allowed_by_robots_runs_normally_and_reads_robots_once():
    report, _, mailbox, fetcher = run_with(make_settings(robots_mode="respect"), make_source(), "User-agent: *\nDisallow: /admin/\n")
    assert report.failed_sources == 0 and len(mailbox.sent) == 1
    assert fetcher.calls.count(ROBOTS_URL) == 1


# --- диагностика ---


def test_check_sources_reports_robots_verdict(capsys):
    pages = {ROBOTS_URL: DENY_INFO, INDEX_URL: index_html([20]), item_url(20): item_html(20)}
    assert check_sources([make_source()], FakeFetcher(pages), "respect") == 1
    out = capsys.readouterr().out
    assert "ЗАПРЕЩАЕТ" in out and "robots: warn" in out

    assert check_sources([make_source(robots="warn")], FakeFetcher(pages), "respect") == 0  # решение пользователя уважено
    out = capsys.readouterr().out
    assert "запрещает загрузку" in out and "режим warn" in out and "Все источники читаются" in out

    pages[ROBOTS_URL] = "User-agent: *\nCrawl-delay: 10\n"
    assert check_sources([make_source()], FakeFetcher(pages), "respect") == 0
    out = capsys.readouterr().out
    assert "загрузка разрешена" in out and "паузу между запросами: 10" in out
