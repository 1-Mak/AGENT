"""Ядро: обнаружить изменения -> проанализировать -> отправить дайджест -> только потом запомнить.

Состояние записывается в базу после успешной отправки письма. Если отправка упала,
изменения будут найдены заново при следующем запуске — ничего не теряется.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .config import Settings
from .extract import extract_document, extract_links, parse_sitemap
from .fetch import FetchError
from .models import Change, DocRecord, Source
from .notify import importance_rank, render
from .store import Store

log = logging.getLogger(__name__)

MAX_CHILD_SITEMAPS = 20


class SourceError(Exception):
    """Источник не удалось проверить (нет ссылок, сломалась вёрстка, блокировка и т.п.)."""


class PageFetcher(Protocol):
    def get(self, url: str) -> str: ...


class ChangeAnalyzer(Protocol):
    def analyze(self, change: Change): ...


@dataclass
class SourceResult:
    source: Source
    changes: list[Change] = field(default_factory=list)
    silent: list[DocRecord] = field(default_factory=list)  # запоминаем без уведомления
    baseline: bool = False


@dataclass
class RunReport:
    changes: list[Change]
    alerts: list[str]
    failed_sources: int
    total_sources: int
    sent: bool


# --- обнаружение документов ---


def discover(source: Source, fetcher: PageFetcher) -> list[str]:
    """Адреса документов источника, от новых к старым (насколько это известно из страницы)."""
    if source.type == "list":
        return extract_links(fetcher.get(source.url), source.url, source.link_pattern)

    # sitemap
    rx = re.compile(source.link_pattern)
    is_index, entries = parse_sitemap(fetcher.get(source.url))
    if is_index:
        children = [loc for loc, _ in entries][:MAX_CHILD_SITEMAPS]
        entries = []
        for child in children:
            _, child_entries = parse_sitemap(fetcher.get(child))
            entries.extend(child_entries)
    entries = [(loc, mod) for loc, mod in entries if rx.search(loc)]
    if entries and all(mod for _, mod in entries):
        entries.sort(key=lambda e: e[1], reverse=True)  # свежие первыми
    return list(dict.fromkeys(loc for loc, _ in entries))


def _fetch_record(source: Source, url: str, fetcher: PageFetcher) -> DocRecord:
    html_text = fetcher.get(url)
    title, text = extract_document(html_text, url, source.content_selector, source.ignore_regex)
    if not text.strip():
        raise SourceError(f"{url}: на странице не найден текст (вёрстка изменилась или антибот-заглушка?)")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return DocRecord(url, source.id, title, digest, text)


def make_diff(old: str, new: str) -> str:
    return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), "было", "стало", lineterm="", n=2))


def _classify(res: SourceResult, store: Store, record: DocRecord) -> None:
    old = store.get_document(record.url)
    if old is None:
        if res.baseline and res.source.initial_notify == 0:
            res.silent.append(record)
        else:
            res.changes.append(Change(res.source, record, "new"))
    elif old.content_hash == "":
        res.silent.append(record)  # URL был известен без текста — это первый снимок, не изменение
    elif old.content_hash != record.content_hash:
        res.changes.append(Change(res.source, record, "updated", diff=make_diff(old.text, record.text)))


def check_source(source: Source, fetcher: PageFetcher, store: Store) -> SourceResult:
    baseline = not store.source_state(source.id).baselined
    res = SourceResult(source, baseline=baseline)

    if source.type == "page":
        _classify(res, store, _fetch_record(source, source.url, fetcher))
        return res

    urls = discover(source, fetcher)
    if not urls:
        raise SourceError(
            f"{source.url}: не найдено ни одной ссылки по шаблону {source.link_pattern!r} — "
            "вероятно, изменилась вёрстка, список подгружается скриптом или сайт отдал заглушку"
        )

    if baseline:
        # Первый запуск: весь архив запоминаем молча (без загрузки страниц), свежие initial_notify — присылаем.
        to_fetch = urls[: source.initial_notify]
        res.silent += [DocRecord(u, source.id, "", "", "") for u in urls[source.initial_notify :]]
    else:
        known = store.known_urls(source.id)
        new_urls = [u for u in urls if u not in known]
        to_fetch = new_urls[: source.max_new_per_run]
        if len(new_urls) > len(to_fetch):
            log.warning(
                "[%s] новых документов %d, за этот запуск берём %d, остальные — в следующий",
                source.id, len(new_urls), len(to_fetch),
            )
        to_fetch += [u for u in urls[: source.recheck_latest] if u in known and u not in to_fetch]

    errors = 0
    for url in to_fetch:
        try:
            _classify(res, store, _fetch_record(source, url, fetcher))
        except (FetchError, SourceError) as e:
            errors += 1
            log.warning("[%s] пропускаю %s: %s", source.id, url, e)
    if to_fetch and errors == len(to_fetch):
        raise SourceError(f"не удалось загрузить ни один из {errors} документов")
    return res


# --- запуск ---


def run(
    sources: list[Source],
    *,
    fetcher: PageFetcher,
    store: Store,
    analyzer: ChangeAnalyzer,
    mailer: Callable[[str, str, str], None],
    settings: Settings,
    dry_run: bool = False,
    out: Callable[[str], None] = print,
) -> RunReport:
    results: list[SourceResult] = []
    alerts: list[str] = []
    alerted_ids: list[str] = []
    failed = 0

    for source in sources:
        try:
            results.append(check_source(source, fetcher, store))
        except (SourceError, FetchError) as e:
            failed += 1
            log.error("[%s] сбой проверки: %s", source.id, e)
            if dry_run:
                continue
            count = store.record_failure(source.id, str(e))
            if count >= settings.failure_alert_threshold and not store.source_state(source.id).alerted:
                alerts.append(f"«{source.name}»: {count} запусков подряд не удаётся проверить. Причина: {e}")
                alerted_ids.append(source.id)

    changes = [c for r in results for c in r.changes]
    for change in changes:
        change.analysis = analyzer.analyze(change)
    changes.sort(key=importance_rank)

    sent = False
    if changes or alerts:
        subject, text, html_body = render(changes, alerts)
        if dry_run:
            out(f"Тема: {subject}\n\n{text}")
        else:
            mailer(subject, text, html_body)  # при ошибке отправки исключение — состояние не сохраняется
            sent = True

    if not dry_run:
        for r in results:
            store.save_records(r.silent + [c.record for c in r.changes])
            store.mark_success(r.source.id)
        for sid in alerted_ids:
            store.mark_alerted(sid)

    log.info("Готово: источников %d (сбоев %d), обновлений %d", len(sources), failed, len(changes))
    return RunReport(changes, alerts, failed, len(sources), sent)
