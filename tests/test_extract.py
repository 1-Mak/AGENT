import pytest

from sitewatch.extract import extract_document, extract_links, normalize_url, parse_sitemap
from tests.helpers import BASE, INDEX_URL, PATTERN, index_html, item_html, item_url


def test_normalize_url_punycode_and_cyrillic_path():
    assert normalize_url("https://честныйзнак.рф/info/#top") == "https://xn--80ajghhoc2aj1c8b.xn--p1ai/info/"
    # уже закодированный путь не кодируется повторно
    once = normalize_url("https://example.com/путь/страница")
    assert normalize_url(once) == once
    assert "%D0%BF" in once


def test_extract_links_filters_dedupes_and_keeps_order():
    links = extract_links(index_html([20, 13, 6]), INDEX_URL, PATTERN)
    # ссылка-дубль с юникодным хостом схлопнулась, пагинация и меню отфильтрованы
    assert links == [item_url(20), item_url(13), item_url(6)]


def test_extract_document_cleans_page_and_keeps_file_links():
    title, text = extract_document(item_html(20), item_url(20))
    assert title == "Что нового в системе с 20.04.2026"
    assert "Добавлено новое требование" in text
    assert "- Пункт один" in text and "- Пункт два" in text
    assert f"[{BASE}/upload/spec_20.pdf]" in text
    assert "Меню сайта" not in text and "Честный ЗНАК" not in text.replace("Что нового", "")
    assert "counter" not in text  # скрипт вырезан


def test_extract_document_is_stable_and_respects_ignore_regex():
    html = "<main><h1>T</h1><p>Текст</p><p>Просмотров: 17</p></main>"
    _, a = extract_document(html, "https://x.ru/a", ignore_regex=(r"^Просмотров: \d+",))
    _, b = extract_document(html.replace("17", "18"), "https://x.ru/a", ignore_regex=(r"^Просмотров: \d+",))
    assert a == b == "T\nТекст"


def test_extract_document_selector_wins_over_main():
    html = "<body><main>Общее</main><div class='doc'><p>Только это</p></div></body>"
    _, text = extract_document(html, "https://x.ru/a", selector=".doc")
    assert text == "Только это"


def test_parse_sitemap_and_index():
    urlset = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://x.ru/a/</loc><lastmod>2026-04-01</lastmod></url>
      <url><loc>https://x.ru/b/</loc></url></urlset>"""
    is_index, entries = parse_sitemap(urlset)
    assert not is_index and entries == [("https://x.ru/a/", "2026-04-01"), ("https://x.ru/b/", "")]

    index = '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://x.ru/s1.xml</loc></sitemap></sitemapindex>'
    is_index, entries = parse_sitemap(index)
    assert is_index and entries == [("https://x.ru/s1.xml", "")]


@pytest.mark.parametrize("bad", ["", "<html>не xml"])
def test_parse_sitemap_rejects_non_xml(bad):
    with pytest.raises(Exception):
        parse_sitemap(bad)
