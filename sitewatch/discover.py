"""Разведка адреса: что за ссылки на странице и какой link_pattern подойдёт для источника.

Нужна, когда адрес раздела неизвестен или отвечает 404: команда показывает группы ссылок, готовые шаблоны для
sources.yaml, PDF и sitemap сайта. Ничего не сохраняет и в конфиг не пишет.
"""

from __future__ import annotations

from collections import defaultdict
from urllib.parse import urlsplit

from . import ui
from .extract import extract_document, extract_links, extract_pdf_links, normalize_url
from .fetch import FetchError
from .pipeline import PageFetcher
from .robots import RobotsPolicy

MAX_GROUPS = 10
FEW_LINKS = 5


def _host(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _suggest_pattern(parent: str) -> str:
    """Шаблон «любая страница прямо в этой папке»: так пишут link_pattern для списка статей."""
    return parent.replace(".", r"\.") + "/[^/?#]+/?$"


def link_groups(links: list[str]) -> list[tuple[str, list[str]]]:
    """Ссылки, сгруппированные по папке (всё, кроме последнего сегмента пути): у статей одного списка папка общая."""
    groups: dict[str, list[str]] = defaultdict(list)
    for link in links:
        path = urlsplit(link).path.rstrip("/")
        if not path or "/" not in path:
            continue
        groups[path.rsplit("/", 1)[0] or "/"].append(link)
    return sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def discover_url(url: str, fetcher: PageFetcher) -> int:
    """Печатает разведку по адресу. Код 1 — страницу получить не удалось."""
    url = normalize_url(url)
    ui.title(f"Разведка: {url}")
    policy = RobotsPolicy(fetcher)
    verdict = policy.check(url)
    if verdict.allowed:
        ui.ok(f"robots.txt: загрузка этого адреса разрешена. {verdict.note}".strip())
    else:
        ui.warn("robots.txt сайта запрещает автоматическую загрузку этого адреса (источнику понадобится robots: warn)")

    sitemaps = policy.sitemaps(url)
    base = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
    if sitemaps:
        ui.ok("sitemap, указанные сайтом в robots.txt (подойдут для type: sitemap, если список подгружается скриптом):")
        for sitemap in sitemaps[:5]:
            ui.info(f"  {sitemap}")
    else:
        try:
            fetcher.get(f"{base}/sitemap.xml")
            ui.ok(f"sitemap по стандартному адресу есть: {base}/sitemap.xml")
        except FetchError:
            ui.info("sitemap в robots.txt не указан и по адресу /sitemap.xml не найден")

    try:
        page = fetcher.get(url)
    except FetchError as e:
        ui.fail(str(e))
        if e.status == 404:
            ui.info("Страницы с таким адресом нет. Откройте раздел в браузере, скопируйте адрес из строки браузера и повторите;")
            ui.info("если раздел есть только в меню сайта, дайте адрес родительской страницы (например, главной страницы раздела).")
        return 1

    title, text = extract_document(page, url)
    ui.ok(f"страница получена: «{title}», {len(page)} символов HTML, текста {len(text)}")
    if len(text) < 200:
        ui.warn("текста очень мало: возможно, это заглушка или антибот, либо страница строится скриптом")

    host = _host(url)
    links = [u for u in extract_links(page, url, ".") if _host(u) == host]
    ui.info(f"ссылок на сайт на странице: {len(links)}")
    if len(links) < FEW_LINKS:
        ui.warn("ссылок мало: список, вероятно, подгружается скриптом. Попробуйте sitemap (см. выше) или адрес другой страницы раздела")

    groups = link_groups([u for u in links if not urlsplit(u).path.lower().endswith(".pdf")])
    if groups:
        ui.title("Группы ссылок (по папке): у статей одного списка папка общая")
        for parent, members in groups[:MAX_GROUPS]:
            example = urlsplit(members[0]).path.rsplit("/", 1)[-1] or members[0]
            ui.info(f"{len(members):4d}  {parent}/   пример: {example}")
            ui.info(f"      link_pattern: '{_suggest_pattern(parent)}'")
        if len(groups) > MAX_GROUPS:
            ui.info(f"... и ещё групп: {len(groups) - MAX_GROUPS}")
        ui.info("Возьмите группу, где лежат нужные статьи, и подставьте её link_pattern в sources.yaml (type: list).")

    main_pdfs = extract_pdf_links(page, url)
    all_pdfs = extract_links(page, url, r"\.pdf(?:$|[?#])")
    ui.title("PDF на странице")
    if all_pdfs:
        ui.info(f"всего ссылок на PDF: {len(all_pdfs)}, из них в основном блоке (их возьмёт follow_pdf): {len(main_pdfs)}")
        for pdf in main_pdfs[:5]:
            ui.info(f"  {pdf}")
        if len(main_pdfs) > 5:
            ui.info(f"  ... и ещё {len(main_pdfs) - 5}")
    else:
        ui.info("PDF-ссылок нет")
    return 0
