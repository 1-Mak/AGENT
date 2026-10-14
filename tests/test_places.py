"""Места в документе: цитаты агента проверяются по тексту, к ним строятся ссылки; письмо ссылается на источник."""

import json
from urllib.parse import unquote

import pytest
from pypdf import PdfReader

from sitewatch.analyze import build_system_prompt
from sitewatch.extract import extract_pdf, extract_pdf_pages
from sitewatch.locate import MAX_PLACES, ground_places, text_fragment
from sitewatch.models import Analysis, Change, DocRecord, Place, Scope
from sitewatch.notify import render
from sitewatch.pipeline import fetch_record, run
from tests.helpers import (
    FakeAnalyzer,
    FakeFetcher,
    Mailbox,
    make_analysis,
    make_change,
    make_settings,
    make_source,
    make_store,
    pdf_bytes,
)

DOC_URL = "https://example.com/news/release-5/"
TEXT = """Что нового в системе с 05.10.2026
Для мясной продукции с переменным весом в отчёте о нанесении теперь обязательно указывается масса нетто.
- Срок перехода — 01.09.2026.
Код | Название | Примечание
Приложение | Таблица атрибутов «мясо» | см. файл [https://example.com/a.pdf]
Прочие изменения: исправлены опечатки в интерфейсе личного кабинета участника оборота."""


def change_with(places, text=TEXT, url=DOC_URL, source_type="page", line_pages=None) -> Change:
    record = DocRecord(url, "s", "Релиз 5", "h", text, line_pages)
    change = Change(make_source(type=source_type, link_pattern=None), record, "new")
    change.analysis = make_analysis()
    change.analysis.places = [Place(**p) if isinstance(p, dict) else p for p in places]
    return change


def grounded(*places, **kw) -> list[Place]:
    change = change_with(list(places), **kw)
    ground_places(change)
    return change.analysis.places


# --- проверка цитат ---


def test_verbatim_quote_is_verified_and_gets_a_text_fragment_link():
    [place] = grounded({"section": "Отчёт о нанесении", "quote": "Для мясной продукции с переменным весом в отчёте о нанесении теперь обязательно указывается масса нетто.", "change": "добавить массу нетто"})
    assert place.verified and place.url.startswith(DOC_URL + "#:~:text=")
    # длинная строка подсвечивается от первых слов до последних: браузер выделит весь абзац
    assert unquote(place.url.split("#:~:text=")[1]) == "Для мясной продукции с переменным,обязательно указывается масса нетто."


@pytest.mark.parametrize(
    "quote",
    [
        "для мясной продукции с переменным весом в отчёте о нанесении теперь обязательно указывается масса нетто",  # регистр, точка в конце
        "Для  мясной продукции с переменным весом\nв отчёте о нанесении теперь обязательно указывается масса нетто.",  # пробелы и перенос
        "Срок перехода — 01.09.2026.",  # длинное тире
        "Срок перехода - 01.09.2026",  # дефис вместо тире
        "- Срок перехода — 01.09.2026.",  # маркер списка
        "+Срок перехода — 01.09.2026.",  # метка из diff
        "Таблица атрибутов мясо",  # без «ёлочек»
        "Для мясной продукции с переменным весом … указывается масса нетто.",  # цитата сокращена многоточием
        "Для мясной продукции с переменным весом ... масса нетто",
    ],
)
def test_harmless_differences_do_not_reject_a_real_quote(quote):
    [place] = grounded({"section": "x", "quote": quote})
    assert place.verified, quote


@pytest.mark.parametrize(
    "quote",
    [
        "Масса нетто теперь не нужна для мясной продукции",  # перефразировано
        "Для мясной продукции с переменным весом … указывается вес брутто",  # конец выдуман
        "масса нетто … для мясной продукции",  # куски в обратном порядке
        "Срок",  # слишком коротко, найдётся где угодно
        "",
    ],
)
def test_invented_or_paraphrased_quote_drops_the_whole_place(quote):
    assert grounded({"section": "Раздел 3", "quote": quote, "change": "сменить срок"}) == []


def test_only_verified_places_survive_among_several():
    places = grounded(
        {"section": "Выдуманный", "quote": "Этого предложения в документе нет", "change": "x"},
        {"section": "Срок", "quote": "Срок перехода — 01.09.2026.", "change": "сменить срок"},
    )
    assert [p.section for p in places] == ["Срок"] and places[0].verified


def test_diff_markers_are_ignored_and_quote_may_come_from_the_part_not_sent_to_the_agent():
    change = change_with([{"section": "x", "quote": "+Прочие изменения: исправлены опечатки в интерфейсе личного кабинета"}])
    ground_places(change)
    assert change.analysis.places[0].verified


def test_at_most_three_places_are_kept():
    places = [{"section": f"Раздел {i}", "quote": "Срок перехода — 01.09.2026."} for i in range(7)]
    assert len(grounded(*places)) == MAX_PLACES == 3


def test_place_found_in_table_cell_links_to_that_cell_and_artifacts_are_not_in_the_fragment():
    [cell, linked] = grounded(
        {"quote": "Таблица атрибутов «мясо»"},
        {"quote": "Приложение | Таблица атрибутов «мясо» | см. файл"},
    )
    assert unquote(cell.url.split("#:~:text=")[1]) == "Таблица атрибутов «мясо»"  # ячейка, а не вся строка с «|»
    assert "|" not in linked.url and "https%3A" not in linked.url and "example.com/a.pdf" not in linked.url  # приписка со ссылкой не попадает


def test_text_fragment_encoding_escapes_hyphen_comma_and_amp_and_shortens_long_lines():
    line = "Срок до 01-09-2026, при этом одни два три четыре пять шесть семь & восемь десять"
    fragment = text_fragment(line)
    start, end = fragment.removeprefix(":~:text=").split(",")  # ровно одна настоящая запятая: между началом и концом
    assert unquote(start) == "Срок до 01-09-2026, при этом" and unquote(end) == "семь & восемь десять"  # первые 5 и последние 4 слова
    assert "%2D" in start and "%2C" in start and "%26" in end  # дефис, запятая и амперсанд экранированы
    assert not any(ch in fragment.removeprefix(":~:text=") for ch in "- &")


def test_text_fragment_of_a_short_line_is_the_whole_line_and_blank_lines_give_nothing():
    assert text_fragment("- Один пункт") == ":~:text=%D0%9E%D0%B4%D0%B8%D0%BD%20%D0%BF%D1%83%D0%BD%D0%BA%D1%82"  # маркер списка отброшен
    assert text_fragment("   ") == "" and text_fragment("") == ""


def test_telegram_text_and_local_files_get_the_plain_document_link():
    for kind, url in (("telegram", "https://t.me/s/chan/12"), ("text", "https://example.com/spec.json"), ("page", "file:///C:/specs/a.txt")):
        [place] = grounded({"quote": "Срок перехода — 01.09.2026."}, url=url, source_type=kind)
        assert place.verified and place.url == url  # якоря там не работают, но цитата подтверждена


def test_grounding_failure_never_breaks_the_letter_and_leaves_no_unchecked_places(monkeypatch, caplog):
    from sitewatch import locate

    monkeypatch.setattr(locate, "_ground", lambda change: 1 / 0)
    change = change_with([{"quote": "Срок перехода — 01.09.2026.", "section": "Срок"}])
    ground_places(change)
    assert change.analysis.places == [] and "Не удалось привязать места" in caplog.text
    _, text, _ = render([change], [])
    assert "Что:" in text and "В документе" not in text  # разбор в письме остался


# --- PDF: ссылка на страницу ---


def test_pdf_lines_know_their_pages_and_text_is_unchanged():
    title, text, pages = extract_pdf_pages(pdf_bytes("guide_v1"), "https://x/g.pdf")
    assert (title, text) == extract_pdf(pdf_bytes("guide_v1"), "https://x/g.pdf")
    lines = text.splitlines()
    assert len(pages) == len(lines) and pages[-1] == 0 and lines[-1].startswith("[Колонтитулы")  # служебная строка без страницы
    reader = PdfReader(__import__("io").BytesIO(pdf_bytes("guide_v1")))
    phrase = "Срок обязательного перехода на формат 1.4 — 01.05.2026."
    real_page = next(i for i, page in enumerate(reader.pages, 1) if phrase in (page.extract_text() or ""))
    assert pages[lines.index(phrase)] == real_page >= 1


def test_pdf_quote_links_to_the_page_not_to_a_text_fragment():
    source = make_source(id="g", type="pdf", url="https://example.com/guide.pdf", link_pattern=None)
    record = fetch_record(source, source.url, FakeFetcher({source.url: pdf_bytes("guide_v1")}))
    assert record.line_pages is not None
    change = Change(source, record, "new")
    change.analysis = make_analysis()
    change.analysis.places = [Place(section="Раздел 3", quote="Срок обязательного перехода на формат 1.4 — 01.05.2026.", change="сменить срок")]
    ground_places(change)
    [place] = change.analysis.places
    assert place.verified and place.page and place.url == f"{source.url}#page={place.page}"


def test_line_pages_are_not_part_of_record_equality_or_the_hash():
    a = DocRecord("u", "s", "t", "h", "x", (1,))
    assert a == DocRecord("u", "s", "t", "h", "x") and "line_pages" not in repr(a)


# --- разбор ответа модели ---


def test_places_from_the_model_are_parsed_leniently():
    answer = {
        "summary": "x", "importance": "высокая",
        "places": [{"section": "Р1", "quote": "цитата для проверки", "change": "менять", "url": "https://evil", "verified": True},
                   "просто строка-цитата", {}, {"section": "  ", "quote": None}, None] ,
    }
    analysis = Analysis.model_validate(answer)
    assert [(p.section, p.quote, p.change) for p in analysis.places] == [("Р1", "цитата для проверки", "менять"), ("", "просто строка-цитата", "")]
    assert Analysis.model_validate({"summary": "x", "importance": "низкая", "places": None}).places == []
    assert Analysis.model_validate({"summary": "x", "importance": "низкая", "places": {"quote": "одна цитата без списка"}}).places[0].quote == "одна цитата без списка"


def test_ground_overwrites_whatever_link_or_verification_the_model_claimed():
    assert grounded({"quote": "выдуманная цитата, которой нет в тексте", "url": "https://evil.example", "verified": True, "page": 99}) == []
    [place] = grounded({"quote": "Срок перехода — 01.09.2026.", "url": "https://evil.example", "page": 99})
    assert place.url.startswith(DOC_URL + "#:~:text=") and place.page is None  # ссылка построена программой, а не взята у модели


def test_prompt_asks_for_places_with_verbatim_quotes():
    prompt = build_system_prompt(Scope(interests="Мясо"))
    assert "places" in prompt and "дословная цитата" in prompt and '"quote"' in prompt and '"section"' in prompt


# --- письмо ---


def letter(places, *, source_name="Честный ЗНАК — релизы", found_on=""):
    change = change_with(places)
    change.found_on = found_on
    ground_places(change)
    return render([change], [], scope=Scope(interests="Мясо"))


def test_letter_has_explicit_source_link_and_pinpoint_places():
    place = {"section": "Отчёт о нанесении", "quote": "Срок перехода — 01.09.2026.", "change": "сменить срок в обработке"}
    subject, text, html_body = letter([place])

    assert f"Источник: Честный ЗНАК — релизы" in text and f"Ссылка:   {DOC_URL}" in text
    assert "В документе:" in text and "- Отчёт о нанесении — «Срок перехода — 01.09.2026.» → сменить срок в обработке" in text
    assert f"{DOC_URL}#:~:text=" in text  # ссылка на место отдельной строкой

    assert f"<a href='{DOC_URL}'>ссылка на документ</a>" in html_body
    assert "<b>В документе</b>" in html_body and "<b>Отчёт о нанесении</b>" in html_body and "открыть место</a>" in html_body
    assert f"href='{DOC_URL}#:~:text=" in html_body


def test_unverified_place_is_not_in_the_letter_at_all():
    _, text, html_body = letter([{"section": "Раздел 7", "quote": "Выдуманная цитата, которой нет", "change": "проверить раздел"}])
    assert "Раздел 7" not in text + html_body and "проверить раздел" not in text + html_body
    assert "В документе" not in text and "В документе" not in html_body and "#:~:text=" not in text
    assert "Ссылка:" in text  # а ссылка на сам источник на месте


def test_pdf_place_shows_page_number_and_pdf_origin_links():
    source = make_source(id="g", type="pdf", url="https://example.com/up/guide.pdf", link_pattern=None)
    record = fetch_record(source, source.url, FakeFetcher({source.url: pdf_bytes("guide_v1")}))
    change = Change(source, record, "new", found_on="https://example.com/page/")
    change.analysis = make_analysis()
    change.analysis.places = [Place(section="Раздел 3", quote="Срок обязательного перехода на формат 1.4 — 01.05.2026.", change="сменить срок")]
    ground_places(change)
    _, text, html_body = render([change], [])
    page = change.analysis.places[0].page
    assert f"(стр. {page})" in text and f"https://example.com/up/guide.pdf#page={page}" in text
    assert "Файл найден на странице: https://example.com/page/" in text
    assert f"href='https://example.com/up/guide.pdf#page={page}'" in html_body and "ссылка на документ" in html_body


def test_html_in_quotes_and_sections_is_escaped():
    text_with_tags = "Строка с тегом <script>alert(1)</script> внутри документа"
    change = change_with([{"section": "<b>Раздел</b>", "quote": text_with_tags, "change": "a & b"}], text=text_with_tags)
    ground_places(change)
    _, _, html_body = render([change], [])
    assert "<script>" not in html_body and "&lt;script&gt;" in html_body and "&lt;b&gt;Раздел&lt;/b&gt;" in html_body and "a &amp; b" in html_body


# --- сквозной запуск ---


def test_run_grounds_places_so_the_sent_letter_contains_verified_links():
    page_url = "https://example.com/requirements/"
    source = make_source(id="req", type="page", url=page_url, link_pattern=None, initial_notify=1)
    html_doc = "<html><body><main><h1>Требования</h1><p>В отчёте о нанесении для мяса теперь обязательно указывается масса нетто.</p></main></body></html>"
    analyzer = FakeAnalyzer(places=[
        Place(section="Отчёт о нанесении", quote="В отчёте о нанесении для мяса теперь обязательно указывается масса нетто.", change="добавить массу нетто"),
        Place(section="Выдуманный раздел", quote="Этого в документе нет вообще никогда", change="ничего"),
    ])
    mailbox = Mailbox()
    report = run([source], fetcher=FakeFetcher({page_url: html_doc}), store=make_store(), analyzer=analyzer, mailer=mailbox, settings=make_settings())
    verified = [p.verified for p in report.changes[0].analysis.places]
    assert verified == [True]  # выдуманное место отброшено ещё до письма
    _, text, html_body = mailbox.sent[0]
    assert f"{page_url}#:~:text=" in text and "Этого в документе нет" not in text + html_body
