"""Разбор HTML: чистый текст документа, ссылки на документы, sitemap."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote, urldefrag, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

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
