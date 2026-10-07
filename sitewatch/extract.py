"""Разбор HTML: чистый текст документа, ссылки на документы, sitemap."""

from __future__ import annotations

import io
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urldefrag, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_DROP_TAGS = [
    "script", "style", "noscript", "nav", "header", "footer", "aside",
    "form", "iframe", "svg", "button", "select",
]  # fmt: skip
_BLOCK_TAGS = [
    "p", "div", "section", "article", "main", "ul", "ol", "table", "tr", "blockquote",
    "pre", "dl", "dt", "dd", "figure", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6",
]  # fmt: skip
_FILE_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".zip", ".rar", ".7z")


def normalize_url(url: str) -> str:
    """Единый вид URL: punycode-хост, процентное кодирование пути, без #фрагмента."""
    url, _ = urldefrag(url.strip())
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    netloc = host
    if parts.port:
        netloc += f":{parts.port}"
    safe = "/%:@!$&'()*+,;=-._~"
    return urlunsplit(
        (parts.scheme.lower(), netloc, quote(parts.path, safe=safe), quote(parts.query, safe=safe + "?"), "")
    )


def extract_links(html: str, base_url: str, pattern: str) -> list[str]:
    """Ссылки, подходящие под pattern, в порядке появления в документе, без дублей."""
    rx = re.compile(pattern)
    soup = BeautifulSoup(html, "lxml")
    found: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        url = normalize_url(urljoin(base_url, href))
        if url in seen or not rx.search(url):
            continue
        seen.add(url)
        found.append(url)
    return found


def parse_sitemap(xml_text: str) -> tuple[bool, list[tuple[str, str]]]:
    """Возвращает (это_индекс_sitemap, [(loc, lastmod), ...])."""
    root = ET.fromstring(xml_text)
    is_index = root.tag.rsplit("}", 1)[-1] == "sitemapindex"
    entries = []
    for node in root:
        loc = lastmod = ""
        for child in node:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "loc":
                loc = (child.text or "").strip()
            elif name == "lastmod":
                lastmod = (child.text or "").strip()
        if loc:
            entries.append((normalize_url(loc), lastmod))
    return is_index, entries


def extract_document(
    html: str,
    base_url: str,
    selector: str | None = None,
    ignore_regex: tuple[str, ...] = (),
) -> tuple[str, str]:
    """Возвращает (заголовок, чистый текст). Текст стабилен между запусками, пока контент не менялся."""
    soup = BeautifulSoup(html, "lxml")

    title = ""
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(" ", strip=True)
    elif soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(" ", strip=True)

    root = None
    if selector:
        root = soup.select_one(selector)
    if root is None:
        root = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"}) or soup.body or soup

    for tag in root.find_all(_DROP_TAGS):
        tag.decompose()

    # Ссылки на файлы (PDF, Word, Excel) — важная часть документации: показываем адрес рядом с текстом.
    for a in root.find_all("a", href=True):
        href = normalize_url(urljoin(base_url, a["href"]))
        if urlsplit(href).path.lower().endswith(_FILE_EXTENSIONS):
            a.append(f" [{href}]")

    for br in root.find_all("br"):
        br.replace_with("\n")
    for li in root.find_all("li"):
        li.insert_before("\n- ")
        li.insert_after("\n")
    for cell in root.find_all(["td", "th"]):
        cell.insert_after(" | ")
    for block in root.find_all(_BLOCK_TAGS):
        block.insert_before("\n")
        block.insert_after("\n")

    ignore = [re.compile(rx) for rx in ignore_regex]
    lines: list[str] = []
    for raw in root.get_text("").splitlines():
        line = re.sub(r"[ \t\r ​]+", " ", raw).strip()
        line = re.sub(r"(?:\s*\|)+\s*$", "", line)  # хвост разделителей таблицы
        if not line or line == "-" or any(rx.search(line) for rx in ignore):
            continue
        lines.append(line)
    return title or base_url, "\n".join(lines)


# --- PDF ---


class ExtractError(Exception):
    """Из файла не удалось получить текст (не PDF, пароль, скан без текстового слоя)."""


# «Страница 3 из 8», «стр. 3», «Page 3 of 8» — явные отметки номера страницы
_PAGE_MARK = re.compile(r"^(?:стр\.?|страница|page)\s*\d+(?:\s*(?:из|of)\s*\d+)?$", re.IGNORECASE)
MIN_PAGES_FOR_HEADER_DETECTION = 6


def is_pdf_url(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(".pdf")


def _clean_lines(text: str) -> list[str]:
    lines = (re.sub(r"[ \t\r ​]+", " ", raw).strip() for raw in text.splitlines())
    return [line for line in lines if line]


def _is_page_number(line: str, page_index: int) -> bool:
    """Одинокое число на краю страницы, близкое к номеру страницы, — это нумерация, а не данные таблицы."""
    return line.isdigit() and len(line) <= 4 and abs(int(line) - (page_index + 1)) <= 15


def extract_pdf(data: bytes, url: str, ignore_regex: tuple[str, ...] = ()) -> tuple[str, str]:
    """Возвращает (заголовок, текст PDF).

    Из текста убирается то, что меняется без изменения смысла и даёт ложные срабатывания:
    номера страниц и колонтитулы, повторяющиеся на большинстве страниц.
    """
    from pypdf import PdfReader  # тяжёлая зависимость нужна только для PDF

    if b"%PDF-" not in data[:1024]:
        raise ExtractError("по этому адресу лежит не PDF (возможно, страница ошибки или антибот-заглушка)")

    started = time.monotonic()
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractError("PDF защищён паролем")
        raw_pages: list[str] = []
        for number, page in enumerate(reader.pages, 1):
            try:
                raw_pages.append(page.extract_text() or "")
            except Exception as e:  # noqa: BLE001 — одна кривая страница не должна ломать весь документ
                log.warning("  PDF: страница %d не прочитана (%s: %s)", number, type(e).__name__, e)
                raw_pages.append("")
    except ExtractError:
        raise
    except Exception as e:  # noqa: BLE001 — сторонний разбор недоверенного файла: любая ошибка = нечитаемый PDF
        raise ExtractError(f"не удалось прочитать PDF ({type(e).__name__}: {e})") from e

    pages = [_clean_lines(text) for text in raw_pages]

    # Номера страниц: явные отметки везде, одинокие числа — только в начале и в конце страницы.
    for index, lines in enumerate(pages):
        lines[:] = [line for line in lines if not _PAGE_MARK.match(line)]
        if lines and _is_page_number(lines[-1], index):
            lines.pop()
        if lines and _is_page_number(lines[0], index):
            lines.pop(0)

    # Колонтитулы: короткие строки, которые встречаются почти на каждой странице. Из страниц они убираются
    # (иначе каждая правка давала бы шум по всему документу), но одной строкой возвращаются в конец текста:
    # так смена версии, указанной только в колонтитуле, не остаётся незамеченной.
    repeated: set[str] = set()
    if len(pages) >= MIN_PAGES_FOR_HEADER_DETECTION:
        seen_on = Counter(line for lines in pages for line in set(lines) if len(line) <= 120)
        threshold = max(3, len(pages) // 2)
        repeated = {line for line, count in seen_on.items() if count >= threshold}
        pages = [[line for line in lines if line not in repeated] for lines in pages]

    ignore = [re.compile(rx) for rx in ignore_regex]
    lines = [line for page in pages for line in page if not any(rx.search(line) for rx in ignore)]
    if not lines:
        raise ExtractError("в PDF нет текста — похоже на скан без текстового слоя (распознавание изображений не поддерживается)")

    title = lines[0] if len(lines[0]) <= 120 else PurePosixPath(unquote(urlsplit(url).path)).name or url
    kept_repeated = sorted(line for line in repeated if not any(rx.search(line) for rx in ignore))
    if kept_repeated:
        lines.append("[Колонтитулы: " + " | ".join(kept_repeated) + "]")
    log.info("  PDF: страниц %d, строк %d, разбор занял %.1f с", len(reader.pages), len(lines), time.monotonic() - started)
    return title, "\n".join(lines)
