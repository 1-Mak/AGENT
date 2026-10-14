"""follow_pdf: PDF, на которые ссылаются страницы, читаются как отдельные документы."""

import logging

import pytest

from sitewatch.analyze import build_prompt
from sitewatch.config import ConfigError, load_sources
from sitewatch.doctor import check_sources
from sitewatch.extract import extract_pdf_links
from sitewatch.fetch import FetchError
from sitewatch.models import Change, DocRecord
from sitewatch.pipeline import check_source, run
from tests.helpers import FakeAnalyzer, FakeFetcher, Mailbox, make_settings, make_source, make_store, pdf_bytes

LIST_URL = "https://example.com/docs/"
ITEM_1 = "https://example.com/docs/item-1/"
ITEM_2 = "https://example.com/docs/item-2/"
PDF_A = "https://example.com/upload/a.pdf"
PDF_B = "https://example.com/upload/b.pdf"
PDF_MENU = "https://example.com/upload/menu.pdf"
PDF_POLICY = "https://example.com/upload/policy.pdf"


def article(*links: str, title: str = "Статья") -> str:
    anchors = "".join(f'<a href="{u}">файл</a>' for u in links)
    return (
        "<html><body><nav><a href='/upload/menu.pdf'>Меню</a></nav>"
        f"<main><h1>{title}</h1><p>Порядок работы с мясной продукцией.</p>{anchors}</main>"
        "<footer><a href='/upload/policy.pdf'>Политика</a></footer></body></html>"
    )


def listing(*items: str) -> str:
    return "<html><body><main>" + "".join(f'<a href="{u}">статья</a>' for u in items) + "</main></body></html>"


def docs_source(**kw):
    params = dict(
        id="docs", name="Документы", type="list", url=LIST_URL, link_pattern=r"/docs/item-\d+/$",
        follow_pdf=True, initial_notify=1, recheck_latest=1,
    )
    params.update(kw)
    return make_source(**params)


def site(**overrides):
    pages = {
        LIST_URL: listing(ITEM_1),
        ITEM_1: article(PDF_A),
        PDF_A: pdf_bytes("guide_v1"),
        PDF_MENU: pdf_bytes("guide_v1"),
        PDF_POLICY: pdf_bytes("guide_v1"),
    }
    pages.update(overrides)
    return FakeFetcher(pages)


# --- поиск ссылок ---


def test_pdf_links_come_from_the_main_block_only_and_without_duplicates():
    html = article(PDF_A, PDF_A, "/upload/b.pdf?rev=2", "mailto:a@b.c", "/about/")
    assert extract_pdf_links(html, ITEM_1) == [PDF_A, "https://example.com/upload/b.pdf?rev=2"]  # меню и подвал не в счёт


def test_pdf_pattern_replaces_the_extension_rule():
    html = article("/download?id=7", "/upload/a.pdf")
    assert extract_pdf_links(html, ITEM_1, pattern=r"/download\?id=") == ["https://example.com/download?id=7"]


def test_content_selector_limits_where_pdf_links_are_searched():
    html = "<body><div class='doc'><a href='/a.pdf'>1</a></div><div class='other'><a href='/b.pdf'>2</a></div></body>"
    assert extract_pdf_links(html, ITEM_1, selector=".doc") == ["https://example.com/a.pdf"]


# --- пайплайн ---


def test_new_page_brings_its_pdf_as_a_separate_document_with_origin():
    store, fetcher = make_store(), site()
    res = check_source(docs_source(), fetcher, store)
    assert [(c.record.url, c.kind, c.found_on) for c in res.changes] == [(ITEM_1, "new", ""), (PDF_A, "new", ITEM_1)]
    pdf_change = res.changes[1]
    assert "Спецификация обмена данными, версия 4.19" in pdf_change.record.title
    assert PDF_MENU not in fetcher.calls and PDF_POLICY not in fetcher.calls  # PDF из меню и подвала не скачиваются


def test_changed_pdf_is_reported_as_updated_even_if_the_page_is_unchanged():
    store, fetcher = make_store(), site()
    first = check_source(docs_source(), fetcher, store)
    store.save_records([c.record for c in first.changes])
    store.mark_success("docs")

    quiet = check_source(docs_source(), fetcher, store)
    assert quiet.changes == []

    fetcher.pages[PDF_A] = pdf_bytes("guide_v2")
    res = check_source(docs_source(), fetcher, store)
    assert [(c.record.url, c.kind, c.found_on) for c in res.changes] == [(PDF_A, "updated", ITEM_1)]
    assert "+Добавлено новое необязательное поле: страна происхождения товара." in res.changes[0].diff


def test_pdf_is_ignored_unless_follow_pdf_is_set():
    fetcher = site()
    res = check_source(docs_source(follow_pdf=False), fetcher, make_store())
    assert [c.record.url for c in res.changes] == [ITEM_1] and PDF_A not in fetcher.calls


def test_max_pdf_links_caps_files_per_page():
    fetcher = site(**{ITEM_1: article(PDF_A, PDF_B), PDF_B: pdf_bytes("guide_v2")})
    res = check_source(docs_source(max_pdf_links=1), fetcher, make_store())
    assert [c.record.url for c in res.changes] == [ITEM_1, PDF_A] and PDF_B not in fetcher.calls


def test_broken_pdf_is_skipped_and_does_not_fail_the_page(caplog):
    fetcher = site(**{PDF_A: FetchError(f"{PDF_A}: HTTP 404", 404)})
    with caplog.at_level(logging.WARNING):
        res = check_source(docs_source(), fetcher, make_store())
    assert [c.record.url for c in res.changes] == [ITEM_1]
    assert any("пропускаю PDF" in r.message and PDF_A in r.message for r in caplog.records)


def test_scan_without_text_layer_is_skipped_too(caplog):
    fetcher = site(**{PDF_A: pdf_bytes("scan")})
    with caplog.at_level(logging.WARNING):
        res = check_source(docs_source(), fetcher, make_store())
    assert [c.record.url for c in res.changes] == [ITEM_1] and "пропускаю PDF" in caplog.text


def test_pdf_linked_from_two_pages_is_read_once_per_run():
    fetcher = site(**{LIST_URL: listing(ITEM_1, ITEM_2), ITEM_2: article(PDF_A, title="Вторая")})
    res = check_source(docs_source(initial_notify=2), fetcher, make_store())
    assert [c.record.url for c in res.changes] == [ITEM_1, PDF_A, ITEM_2]
    assert fetcher.calls.count(PDF_A) == 1


def test_single_page_source_follows_pdfs_and_stays_quiet_on_the_first_run():
    page_url = "https://example.com/requirements/"
    source = make_source(id="req", type="page", url=page_url, link_pattern=None, follow_pdf=True, initial_notify=0, recheck_latest=0)
    fetcher = FakeFetcher({page_url: article(PDF_A), PDF_A: pdf_bytes("guide_v1"), PDF_B: pdf_bytes("guide_v2")})
    store = make_store()

    first = check_source(source, fetcher, store)
    assert first.changes == [] and {r.url for r in first.silent} == {page_url, PDF_A}  # первый снимок запоминается молча
    store.save_records(first.silent)
    store.mark_success("req")

    fetcher.pages[page_url] = article(PDF_A, PDF_B)
    second = check_source(source, fetcher, store)
    assert {(c.record.url, c.kind, c.found_on) for c in second.changes} == {
        (page_url, "updated", ""),
        (PDF_B, "new", page_url),
    }


def test_link_without_pdf_extension_is_read_as_pdf_when_pattern_matches():
    download = "https://example.com/download?id=7"
    source = docs_source(pdf_pattern=r"/download\?id=")
    fetcher = site(**{ITEM_1: article(download), download: pdf_bytes("guide_v1")})
    res = check_source(source, fetcher, make_store())
    assert [c.record.url for c in res.changes] == [ITEM_1, download] and "версия 4.19" in res.changes[1].record.title


def test_letter_and_agent_prompt_say_where_the_pdf_was_found():
    mailbox = Mailbox()
    report = run(
        [docs_source()], fetcher=site(), store=make_store(), analyzer=FakeAnalyzer(), mailer=mailbox, settings=make_settings()
    )
    assert [c.record.url for c in report.changes] == [ITEM_1, PDF_A]
    _, text, _ = mailbox.sent[0]
    assert f"Файл найден на странице: {ITEM_1}" in text

    change = Change(docs_source(), DocRecord(PDF_A, "docs", "Руководство", "h", "Текст"), "new", found_on=ITEM_1)
    prompt, _ = build_prompt(change, 10_000)
    assert f'found_on="{ITEM_1}"' in prompt
    assert "found_on" not in build_prompt(Change(docs_source(), change.record, "new"), 10_000)[0]


# --- конфигурация и проверка источников ---


def test_config_reads_pdf_options_and_rejects_bad_pattern(tmp_path):
    ok = tmp_path / "ok.yaml"
    ok.write_text(
        "sources:\n  - {id: a, type: page, url: 'https://e.com/', follow_pdf: true, pdf_pattern: '/up/', max_pdf_links: 2}\n"
        "  - {id: b, type: page, url: 'https://e.com/b'}\n",
        encoding="utf-8",
    )
    a, b = load_sources(ok)
    assert (a.follow_pdf, a.pdf_pattern, a.max_pdf_links) == (True, "/up/", 2)
    assert (b.follow_pdf, b.pdf_pattern, b.max_pdf_links) == (False, None, 5)

    bad = tmp_path / "bad.yaml"
    bad.write_text("sources:\n  - {id: a, type: page, url: 'https://e.com/', pdf_pattern: '['}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="pdf_pattern"):
        load_sources(bad)


def test_check_sources_lists_pdfs_the_page_links_to(capsys):
    check_sources([docs_source()], site())
    out = capsys.readouterr().out
    assert "PDF-ссылок на странице: 1" in out and PDF_A in out and PDF_MENU not in out


def test_check_sources_warns_when_follow_pdf_finds_nothing(capsys):
    check_sources([docs_source()], site(**{ITEM_1: article()}))
    assert "follow_pdf включён, но PDF-ссылок" in capsys.readouterr().out


def test_archive_pdfs_do_not_flood_the_letter_when_old_pages_are_rechecked_for_the_first_time():
    """Архив при initial_notify=0 запоминается без загрузки страниц; на второй день их первый снимок молчит, и PDF тоже."""
    source = docs_source(initial_notify=0, recheck_latest=2)
    fetcher = site(**{LIST_URL: listing(ITEM_1, ITEM_2), ITEM_2: article(PDF_B, title="Вторая"), PDF_B: pdf_bytes("guide_v2")})
    store = make_store()

    day1 = check_source(source, fetcher, store)
    assert day1.changes == []
    store.save_records(day1.silent)
    store.mark_success("docs")

    day2 = check_source(source, fetcher, store)
    assert day2.changes == []  # ни страницы, ни их PDF в письмо не идут
    assert {r.url for r in day2.silent} == {ITEM_1, ITEM_2, PDF_A, PDF_B}
    store.save_records(day2.silent)

    # настоящая новость: на странице появился ещё один файл — это правка страницы и новый документ
    fetcher.pages[ITEM_2] = article(PDF_B, PDF_MENU, title="Вторая")
    day3 = check_source(source, fetcher, store)
    assert {(c.record.url, c.kind) for c in day3.changes} == {(ITEM_2, "updated"), (PDF_MENU, "new")}


def test_pdfs_closed_by_robots_txt_give_one_short_warning_per_page_and_warn_mode_loads_them(caplog):
    from sitewatch.robots import GuardedFetcher, RobotsPolicy

    robots = "User-agent: *\nDisallow: /upload/\n"
    fetcher = site(**{ITEM_1: article(PDF_A, PDF_B), PDF_B: pdf_bytes("guide_v2"), "https://example.com/robots.txt": robots})

    respect = GuardedFetcher(fetcher, RobotsPolicy(fetcher), "respect")
    with caplog.at_level(logging.WARNING):
        res = check_source(docs_source(), respect, make_store())
    assert [c.record.url for c in res.changes] == [ITEM_1]  # сама страница открыта, файлы из /upload/ — нет
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "запрещает загрузку PDF (2 на странице" in warnings[0] and "robots: warn" in warnings[0]

    caplog.clear()
    permitted = GuardedFetcher(fetcher, RobotsPolicy(fetcher), "warn")  # решение пользователя: robots: warn
    res = check_source(docs_source(), permitted, make_store())
    assert [c.record.url for c in res.changes] == [ITEM_1, PDF_A, PDF_B]
