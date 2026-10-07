"""PDF как источник: извлечение текста, чистка шума, сравнение версий, интеграция в пайплайн."""

import io

import pytest
from pypdf import PdfReader, PdfWriter

from sitewatch.analyze import FULL_TEXT_LIMIT, build_prompt
from sitewatch.extract import ExtractError, _is_page_number, extract_pdf, is_pdf_url
from sitewatch.models import Change, DocRecord
from sitewatch.pipeline import SourceError, check_source, fetch_record, make_diff, run
from tests.helpers import (
    FakeAnalyzer,
    FakeFetcher,
    Mailbox,
    make_settings,
    make_source,
    make_store,
    pdf_bytes,
)

PDF_URL = "https://example.com/upload/guide.pdf"


def pdf_source(**kw):
    params = dict(id="guide", name="Руководство (PDF)", type="pdf", url=PDF_URL, link_pattern=None, initial_notify=0, recheck_latest=0)
    params.update(kw)
    return make_source(**params)


# --- извлечение текста ---


def test_extracts_russian_text_and_strips_headers_and_page_numbers():
    title, text = extract_pdf(pdf_bytes("guide_v1"), PDF_URL)
    assert title == "Спецификация обмена данными, версия 4.19"  # колонтитул не стал заголовком
    assert "Срок обязательного перехода на формат 1.4 — 01.05.2026." in text
    assert "Раздел 8. Описание метода обмена данными." in text
    assert text.count("Руководство участника оборота. Честный ЗНАК") == 1  # не 8 раз, а один раз — в служебной строке
    assert text.splitlines()[-1].startswith("[Колонтитулы:")
    assert "Страница" not in text  # «Страница N из 8»


def test_versions_differ_only_where_the_content_changed():
    old = extract_pdf(pdf_bytes("guide_v1"), PDF_URL)[1]
    new = extract_pdf(pdf_bytes("guide_v2"), PDF_URL)[1]
    diff = make_diff(old, new).splitlines()
    removed = [line for line in diff if line.startswith("-") and not line.startswith("---")]
    added = [line for line in diff if line.startswith("+") and not line.startswith("+++")]
    assert removed == ["-Спецификация обмена данными, версия 4.19", "-Срок обязательного перехода на формат 1.4 — 01.05.2026."]
    assert added == [
        "+Спецификация обмена данными, версия 4.20",
        "+Срок обязательного перехода на формат 1.4 — 01.07.2026.",
        "+Добавлено новое необязательное поле: страна происхождения товара.",
    ]  # ни номера страниц, ни колонтитулы в разнице не участвуют


def test_version_bump_only_in_running_header_is_still_a_visible_change():
    old = extract_pdf(pdf_bytes("guide_v1"), PDF_URL)[1]
    new = extract_pdf(pdf_bytes("guide_v1_header"), PDF_URL)[1]
    diff = [line for line in make_diff(old, new).splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    assert len(diff) == 2 and "Версия 4.20" in diff[1] and diff[0].startswith("-[Колонтитулы:")  # одна строка, а не 8 страниц шума


def test_ignore_regex_can_silence_a_noisy_header():
    _, text = extract_pdf(pdf_bytes("guide_v1_header"), PDF_URL, ignore_regex=(r"Версия \d",))
    assert "Версия 4.20" not in text


def test_short_documents_keep_their_headers():
    reader = PdfReader(io.BytesIO(pdf_bytes("guide_v1")))
    writer = PdfWriter()
    for page in reader.pages[:3]:
        writer.add_page(page)
    buffer = io.BytesIO()
    writer.write(buffer)
    _, text = extract_pdf(buffer.getvalue(), PDF_URL)
    assert "Руководство участника оборота. Честный ЗНАК" in text  # меньше 6 страниц — колонтитулы не угадываем
    assert "[Колонтитулы" not in text


def test_ignore_regex_applies_to_pdf_lines():
    _, text = extract_pdf(pdf_bytes("guide_v1"), PDF_URL, ignore_regex=(r"^Раздел \d+\.",))
    assert "Раздел" not in text and "Срок обязательного" in text


def test_scan_without_text_layer_is_reported_clearly():
    with pytest.raises(ExtractError, match="скан"):
        extract_pdf(pdf_bytes("scan"), PDF_URL)


@pytest.mark.parametrize(
    "data, message",
    [
        (b"<html>Access denied</html>", "не PDF"),
        (b"%PDF-1.4\nthis is not a real pdf", "не удалось прочитать PDF"),
        (b"", "не PDF"),
    ],
)
def test_broken_or_fake_pdfs_give_readable_errors(data, message):
    with pytest.raises(ExtractError, match=message):
        extract_pdf(data, PDF_URL)


def test_page_number_heuristic_does_not_eat_table_values():
    assert _is_page_number("3", 2) and _is_page_number("12", 10)
    assert not _is_page_number("100", 2)  # значение из таблицы
    assert not _is_page_number("2026", 2)
    assert not _is_page_number("3.5", 2)


def test_is_pdf_url():
    assert is_pdf_url("https://x.ru/a/B.PDF") and is_pdf_url("https://x.ru/a.pdf?download=1")
    assert not is_pdf_url("https://x.ru/a.pdf/view") and not is_pdf_url("https://x.ru/doc.html")


# --- пайплайн ---


def test_pdf_source_first_run_is_silent_then_update_is_reported_with_diff():
    store, mailbox = make_store(), Mailbox()

    def do_run(pdf):
        return run([pdf_source()], fetcher=FakeFetcher({PDF_URL: pdf}), store=store, analyzer=FakeAnalyzer(),
                   mailer=mailbox, settings=make_settings())

    assert do_run(pdf_bytes("guide_v1")).changes == [] and mailbox.sent == []  # объёмный документ молча запоминается
    assert do_run(pdf_bytes("guide_v1")).changes == []

    report = do_run(pdf_bytes("guide_v2"))
    assert [c.kind for c in report.changes] == ["updated"]
    assert "+Срок обязательного перехода на формат 1.4 — 01.07.2026." in report.changes[0].diff
    assert len(mailbox.sent) == 1 and "версия 4.20" in mailbox.sent[0][1]


def test_same_pdf_with_different_bytes_is_not_a_change():
    """Метаданные и время создания меняются при каждой пересборке файла — смысл тот же."""
    store, mailbox = make_store(), Mailbox()
    first = pdf_bytes("guide_v1")
    rebuilt = first.replace(b"/Producer", b"/Prodycer", 1) if b"/Producer" in first else first + b"\n%% rebuilt"
    assert rebuilt != first

    for data in (first, rebuilt):
        run([pdf_source()], fetcher=FakeFetcher({PDF_URL: data}), store=store, analyzer=FakeAnalyzer(),
            mailer=mailbox, settings=make_settings())
    assert mailbox.sent == []


def test_list_source_with_pdf_links_downloads_them_as_pdf():
    index = "https://example.com/docs/"
    page = '<main><a href="/upload/a.pdf">A</a><a href="/upload/b.pdf">B</a><a href="/about/">О нас</a></main>'
    pages = {index: page, "https://example.com/upload/a.pdf": pdf_bytes("guide_v1"), "https://example.com/upload/b.pdf": pdf_bytes("guide_v2")}
    source = make_source(id="docs", url=index, link_pattern=r"\.pdf$", initial_notify=1, recheck_latest=0)
    store, mailbox = make_store(), Mailbox()
    report = run([source], fetcher=FakeFetcher(pages), store=store, analyzer=FakeAnalyzer(), mailer=mailbox, settings=make_settings())
    assert [c.record.url for c in report.changes] == ["https://example.com/upload/a.pdf"]
    assert "версия 4.19" in report.changes[0].record.title
    assert store.known_urls("docs") == {"https://example.com/upload/a.pdf", "https://example.com/upload/b.pdf"}


def test_scan_pdf_fails_the_source_with_a_clear_reason():
    with pytest.raises(SourceError, match="скан"):
        check_source(pdf_source(), FakeFetcher({PDF_URL: pdf_bytes("scan")}), make_store())


def test_html_stub_instead_of_pdf_is_a_source_error():
    with pytest.raises(SourceError, match="не PDF"):
        fetch_record(pdf_source(), PDF_URL, FakeFetcher({PDF_URL: b"<html>captcha</html>"}))


# --- что получает агент ---


def change(kind, text, diff=""):
    return Change(pdf_source(), DocRecord(PDF_URL, "guide", "Руководство", "h", text), kind, diff=diff)


def test_big_updated_document_goes_to_agent_as_diff_only():
    text = "строка документа\n" * (FULL_TEXT_LIMIT // 10)
    prompt, truncated = build_prompt(change("updated", text, diff="-старое\n+новое"), 120_000)
    assert "полный текст не приводится" in prompt and "+новое" in prompt
    assert "строка документа" not in prompt and truncated is False


def test_small_updated_and_new_documents_keep_full_text():
    prompt, _ = build_prompt(change("updated", "короткий текст", diff="-а\n+б"), 120_000)
    assert "короткий текст" in prompt
    big = "строка документа\n" * (FULL_TEXT_LIMIT // 10)
    prompt, _ = build_prompt(change("new", big), 120_000)
    assert "строка документа" in prompt  # новый документ агенту нужно показать целиком
