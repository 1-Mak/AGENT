import pytest

from sitewatch.fetch import FetchError
from sitewatch.pipeline import SourceError, check_source, run
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


def pages_for(numbers, **overrides):
    pages = {INDEX_URL: index_html(numbers)}
    pages.update({item_url(n): item_html(n) for n in numbers})
    pages.update(overrides)
    return pages


def do_run(source, pages, store, mailbox, analyzer=None, **kwargs):
    return run(
        [source],
        fetcher=FakeFetcher(pages),
        store=store,
        analyzer=analyzer or FakeAnalyzer(),
        mailer=mailbox,
        settings=make_settings(),
        **kwargs,
    )


def test_first_run_notifies_only_newest_and_remembers_archive_silently():
    store, mailbox, fetcher = make_store(), Mailbox(), None
    fetcher = FakeFetcher(pages_for([20, 13, 6]))
    report = run(
        [make_source()], fetcher=fetcher, store=store, analyzer=FakeAnalyzer(), mailer=mailbox, settings=make_settings()
    )
    assert [c.record.url for c in report.changes] == [item_url(20)]
    assert len(mailbox.sent) == 1
    # страницы архива не скачивались, но адреса запомнены
    assert fetcher.calls == [INDEX_URL, item_url(20)]
    assert store.known_urls("cz-releases") == {item_url(20), item_url(13), item_url(6)}


def test_second_run_without_changes_sends_nothing():
    store, mailbox = make_store(), Mailbox()
    pages = pages_for([20, 13, 6])
    do_run(make_source(), pages, store, mailbox)
    mailbox.sent.clear()
    report = do_run(make_source(), pages, store, mailbox)
    assert report.changes == [] and mailbox.sent == []


def test_new_release_is_detected_analyzed_and_emailed():
    store, mailbox, analyzer = make_store(), Mailbox(), FakeAnalyzer("высокая")
    do_run(make_source(), pages_for([13, 6]), store, mailbox)
    mailbox.sent.clear()

    report = do_run(make_source(), pages_for([20, 13, 6]), store, mailbox, analyzer)
    assert [(c.kind, c.record.url) for c in report.changes] == [("new", item_url(20))]
    assert analyzer.seen == [item_url(20)]
    subject, text, html_body = mailbox.sent[0]
    assert "Что нового в системе с 20.04.2026" in subject
    assert "[ВЫСОКАЯ]" in text and "Разбор:" in text
    assert "высокая" in html_body


def test_edit_of_recent_page_is_reported_as_update_with_diff():
    store, mailbox = make_store(), Mailbox()
    pages = pages_for([20, 13])
    do_run(make_source(), pages, store, mailbox)
    do_run(make_source(), pages, store, mailbox)  # 2-й запуск: подхватывает текст 13-го (первый снимок), без писем
    mailbox.sent.clear()

    edited = pages_for([20, 13], **{item_url(13): item_html(13, body="Срок перенесён на 1 июля.")})
    report = do_run(make_source(), edited, store, mailbox)
    assert [(c.kind, c.record.url) for c in report.changes] == [("updated", item_url(13))]
    assert "+Срок перенесён на 1 июля." in report.changes[0].diff
    assert "-Добавлено новое требование" in report.changes[0].diff


def test_url_only_baseline_entry_gets_text_silently_not_as_update():
    store, mailbox = make_store(), Mailbox()
    pages = pages_for([20, 13])
    do_run(make_source(), pages, store, mailbox)  # 13 запомнен без текста
    mailbox.sent.clear()
    report = do_run(make_source(), pages, store, mailbox)  # recheck_latest=2 → скачали текст 13
    assert report.changes == [] and mailbox.sent == []
    assert store.get_document(item_url(13)).content_hash != ""


def test_failed_send_loses_nothing():
    store = make_store()
    do_run(make_source(), pages_for([13, 6]), store, Mailbox())
    with pytest.raises(ConnectionError):
        do_run(make_source(), pages_for([20, 13, 6]), store, Mailbox(fail=True))
    assert item_url(20) not in store.known_urls("cz-releases")

    mailbox = Mailbox()
    report = do_run(make_source(), pages_for([20, 13, 6]), store, mailbox)
    assert [c.record.url for c in report.changes] == [item_url(20)] and len(mailbox.sent) == 1


def test_dry_run_prints_but_neither_sends_nor_saves():
    store, mailbox, printed = make_store(), Mailbox(), []
    report = do_run(make_source(), pages_for([20, 13]), store, mailbox, dry_run=True, out=printed.append)
    assert mailbox.sent == [] and report.sent is False
    assert printed and "Что нового в системе с 20.04.2026" in printed[0]
    assert store.known_urls("cz-releases") == set()


def test_flood_guard_limits_new_documents_per_run():
    store, mailbox = make_store(), Mailbox()
    do_run(make_source(max_new_per_run=2), pages_for([1]), store, mailbox)
    mailbox.sent.clear()
    report = do_run(make_source(max_new_per_run=2), pages_for([10, 9, 8, 7, 1]), store, mailbox)
    assert len(report.changes) == 2
    report = do_run(make_source(max_new_per_run=2), pages_for([10, 9, 8, 7, 1]), store, mailbox)
    assert len(report.changes) == 2  # оставшиеся приходят в следующем запуске


def test_empty_list_page_is_a_source_error_not_silence():
    store = make_store()
    fetcher = FakeFetcher({INDEX_URL: "<html><body>Проверка браузера...</body></html>"})
    with pytest.raises(SourceError, match="не найдено ни одной ссылки"):
        check_source(make_source(), fetcher, store)


def test_alert_after_threshold_is_sent_once_and_reset_on_recovery():
    store, mailbox = make_store(), Mailbox()
    down = {INDEX_URL: FetchError("HTTP 403")}
    for _ in range(2):
        do_run(make_source(), down, store, mailbox)
    assert mailbox.sent == []  # порог 3 ещё не достигнут

    do_run(make_source(), down, store, mailbox)
    assert len(mailbox.sent) == 1 and "Сбой" in mailbox.sent[0][0] and "HTTP 403" in mailbox.sent[0][1]

    do_run(make_source(), down, store, mailbox)
    assert len(mailbox.sent) == 1  # повторно не спамим

    do_run(make_source(), pages_for([20, 13]), store, mailbox)  # восстановление
    assert store.source_state("cz-releases").failures == 0
    do_run(make_source(), down, store, mailbox)
    assert store.source_state("cz-releases").alerted is False


def test_digest_warns_when_analyst_is_down_for_example_no_balance():
    store, mailbox = make_store(), Mailbox()
    reason = "на балансе DeepSeek недостаточно средств"
    do_run(make_source(), pages_for([20, 13]), store, mailbox, FakeAnalyzer(disabled_reason=reason))
    subject, text, _ = mailbox.sent[0]
    assert "Агент-аналитик не работает" in text and reason in text


def test_one_broken_item_does_not_block_the_others():
    store, mailbox = make_store(), Mailbox()
    do_run(make_source(), pages_for([6]), store, mailbox)
    mailbox.sent.clear()
    pages = pages_for([20, 13, 6], **{item_url(13): FetchError("HTTP 500")})
    report = do_run(make_source(), pages, store, mailbox)
    assert [c.record.url for c in report.changes] == [item_url(20)]
    assert item_url(13) not in store.known_urls("cz-releases")  # будет повторная попытка в следующий раз


def test_page_source_detects_change():
    store, mailbox = make_store(), Mailbox()
    src = make_source(id="req", type="page", url="https://x.ru/req/", link_pattern=None, initial_notify=0)
    page = lambda body: f"<main><h1>Требования</h1><p>{body}</p></main>"  # noqa: E731
    do_run(src, {src.url: page("Версия 1")}, store, mailbox)
    assert mailbox.sent == []  # первый запуск молча запоминает страницу

    report = do_run(src, {src.url: page("Версия 2")}, store, mailbox)
    assert [c.kind for c in report.changes] == ["updated"]
    assert len(mailbox.sent) == 1


def test_sitemap_source_discovers_and_sorts_by_lastmod():
    store, mailbox = make_store(), Mailbox()
    sm_url = "https://xn--80ajghhoc2aj1c8b.xn--p1ai/sitemap.xml"
    sitemap = f"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>{item_url(6)}</loc><lastmod>2026-04-01</lastmod></url>
      <url><loc>{item_url(20)}</loc><lastmod>2026-04-24</lastmod></url>
      <url><loc>{INDEX_URL}</loc><lastmod>2026-04-24</lastmod></url></urlset>"""
    src = make_source(id="cz-sm", type="sitemap", url=sm_url, initial_notify=1)
    report = do_run(src, {sm_url: sitemap, item_url(20): item_html(20)}, store, mailbox)
    assert [c.record.url for c in report.changes] == [item_url(20)]  # самый свежий по lastmod
    assert store.known_urls("cz-sm") == {item_url(20), item_url(6)}
