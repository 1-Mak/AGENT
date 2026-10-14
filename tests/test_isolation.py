"""Сломавшийся или неработающий источник не мешает остальным: запуск идёт дальше, из него просто нет новостей."""

import dataclasses
import logging

from sitewatch.models import Source
from sitewatch.pipeline import run
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

SITE = "https://broken.example.com"


def broken_sources():
    sources = [
        make_source(id="working", initial_notify=1),
        make_source(id="not-found", type="page", url=f"{SITE}/nope/", link_pattern=None),
        make_source(id="bad-sitemap", type="sitemap", url=f"{SITE}/sitemap.xml", link_pattern=r"/a"),
        make_source(id="no-links", type="list", url=f"{SITE}/empty/", link_pattern=r"/zzz/"),
        make_source(id="no-file", type="text", url="file:///нет/такого/файла.json", link_pattern=None),
        make_source(id="parser-bug", type="page", url=f"{SITE}/boom/", link_pattern=None),
        make_source(id="robots-closed", type="list", url=f"{SITE}/closed/", link_pattern=r"/closed/x"),
    ]
    return [dataclasses.replace(s, name=f"«{s.id}»") for s in sources]  # в письме источник называют по имени


def broken_site():
    return FakeFetcher(
        {
            INDEX_URL: index_html([20]),
            item_url(20): item_html(20),
            f"{SITE}/sitemap.xml": "<html>это не XML, а заглушка антибота",
            f"{SITE}/empty/": "<html><body><main>нет ссылок</main></body></html>",
            f"{SITE}/boom/": RuntimeError("что-то непредвиденное в разборе"),
            f"{SITE}/robots.txt": "User-agent: *\nDisallow: /closed/\n",
            f"{SITE}/closed/": "<html><body><a href='/closed/x1'>1</a></body></html>",
        }
    )


def do_run(store, mailbox=None, **kw):
    return run(
        broken_sources(), fetcher=broken_site(), store=store, analyzer=FakeAnalyzer(), mailer=mailbox or Mailbox(),
        settings=make_settings(robots_mode="respect"), **kw,
    )


def test_every_kind_of_broken_source_is_skipped_and_the_working_one_still_delivers(caplog):
    store, mailbox = make_store(), Mailbox()
    with caplog.at_level(logging.ERROR):
        report = do_run(store, mailbox)

    assert report.total_sources == 7 and report.failed_sources == 6
    assert [c.record.url for c in report.changes] == [item_url(20)]  # из рабочего источника письмо пришло
    assert len(mailbox.sent) == 1 and store.source_state("working").baselined  # и его состояние сохранено
    for broken in ("not-found", "bad-sitemap", "no-links", "no-file", "parser-bug", "robots-closed"):
        assert store.source_state(broken).failures == 1 and not store.source_state(broken).baselined, broken
    reasons = {r.message for r in caplog.records}
    assert any("sitemap не читается как XML" in m for m in reasons)
    assert any("непредвиденная ошибка разбора (RuntimeError: что-то непредвиденное" in m for m in reasons)


def test_persistent_failures_become_one_alert_per_source_and_the_good_source_is_unaffected():
    store = make_store()
    for _ in range(2):
        report = do_run(store)
        assert report.alerts == []
    third = do_run(store, mailbox := Mailbox())
    assert len(third.alerts) == 6 and any("««bad-sitemap»»" in a and "XML" in a for a in third.alerts)
    assert any("непредвиденная ошибка разбора" in a for a in third.alerts)
    assert len(mailbox.sent) == 1 and "Сбои мониторинга" in mailbox.sent[0][1]  # одно письмо с понятным списком

    fourth = do_run(store, mailbox := Mailbox())
    assert fourth.alerts == [] and mailbox.sent == []  # повторно не шумим, пока причина та же


def test_all_sources_failing_is_reported_through_the_failure_counter_not_a_crash():
    store = make_store()
    only_broken = [s for s in broken_sources() if s.id != "working"]
    report = run(only_broken, fetcher=broken_site(), store=store, analyzer=FakeAnalyzer(), mailer=Mailbox(), settings=make_settings())
    assert report.failed_sources == report.total_sources == 6 and report.changes == []


def test_dry_run_with_broken_sources_does_not_touch_the_failure_counters():
    store = make_store()
    do_run(store, dry_run=True)
    assert store.source_state("not-found").failures == 0
