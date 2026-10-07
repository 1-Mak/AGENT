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


def fetch_record(source: Source, url: str, fetcher: PageFetcher) -> DocRecord:
    html_text = fetcher.get(url)
    title, text = extract_document(html_text, url, source.content_selector, source.ignore_regex)
    if not text.strip():
        raise SourceError(f"{url}: на странице не найден текст (вёрстка изменилась или антибот-заглушка?)")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    log.info("  извлечено: «%s», %d символов", title, len(text))
    log.debug("  хэш текста: %s", digest[:12])
    return DocRecord(url, source.id, title, digest, text)


def make_diff(old: str, new: str) -> str:
    return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), "было", "стало", lineterm="", n=2))


def _classify(res: SourceResult, store: Store, record: DocRecord) -> None:
    old = store.get_document(record.url)
    if old is None:
        if res.baseline and res.source.initial_notify == 0:
            log.info("  первый снимок страницы: запоминаю молча")
            res.silent.append(record)
        else:
            log.info("  НОВЫЙ документ -> войдёт в письмо")
            res.changes.append(Change(res.source, record, "new"))
    elif old.content_hash == "":
        log.info("  первый снимок текста (адрес был известен): запоминаю молча")
        res.silent.append(record)  # URL был известен без текста — это первый снимок, не изменение
    elif old.content_hash != record.content_hash:
        log.info("  ИЗМЕНЁН -> войдёт в письмо")
        res.changes.append(Change(res.source, record, "updated", diff=make_diff(old.text, record.text)))
    else:
        log.info("  без изменений")


def check_source(source: Source, fetcher: PageFetcher, store: Store) -> SourceResult:
    baseline = not store.source_state(source.id).baselined
    res = SourceResult(source, baseline=baseline)
    log.info("[%s] проверяю %s (тип: %s)", source.id, source.url, source.type)

    if source.type == "page":
        _classify(res, store, fetch_record(source, source.url, fetcher))
        return res

    urls = discover(source, fetcher)
    if not urls:
        raise SourceError(
            f"{source.url}: не найдено ни одной ссылки по шаблону {source.link_pattern!r} — "
            "вероятно, изменилась вёрстка, список подгружается скриптом или сайт отдал заглушку "
            "(посмотрите, что видит парсер: sitewatch check-sources)"
        )

    if baseline:
        # Первый запуск: весь архив запоминаем молча (без загрузки страниц), свежие initial_notify — присылаем.
        log.info(
            "[%s] первый запуск: найдено документов %d; архив запоминаю молча, в письмо возьму самых свежих: %d",
            source.id, len(urls), min(source.initial_notify, len(urls)),
        )
        to_fetch = urls[: source.initial_notify]
        res.silent += [DocRecord(u, source.id, "", "", "") for u in urls[source.initial_notify :]]
    else:
        known = store.known_urls(source.id)
        new_urls = [u for u in urls if u not in known]
        log.info("[%s] найдено документов: %d, из них новых: %d", source.id, len(urls), len(new_urls))
        to_fetch = new_urls[: source.max_new_per_run]
        if len(new_urls) > len(to_fetch):
            log.warning(
                "[%s] новых документов %d, за этот запуск берём %d, остальные — в следующий",
                source.id, len(new_urls), len(to_fetch),
            )
        recheck = [u for u in urls[: source.recheck_latest] if u in known and u not in to_fetch]
        if recheck:
            log.info("[%s] перепроверяю на правки последние известные: %d", source.id, len(recheck))
        to_fetch += recheck

    errors = 0
    for url in to_fetch:
        log.info("[%s] документ %s", source.id, url)
        try:
            _classify(res, store, fetch_record(source, url, fetcher))
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
    log.info(
        "Запуск: источников %d, режим: %s",
        len(sources), "ПРОБНЫЙ (ничего не отправляется и не сохраняется)" if dry_run else "боевой",
    )
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
            log.error("[%s] сбоев подряд: %d (письмо-алерт при %d)", source.id, count, settings.failure_alert_threshold)
            if count >= settings.failure_alert_threshold and not store.source_state(source.id).alerted:
                alerts.append(f"«{source.name}»: {count} запусков подряд не удаётся проверить. Причина: {e}")
                alerted_ids.append(source.id)

    changes = [c for r in results for c in r.changes]
    for change in changes:
        log.info("Агент разбирает: «%s» (%s)", change.record.title, "новый" if change.kind == "new" else "изменён")
        change.analysis = analyzer.analyze(change)
        if change.analysis:
            log.info("  разбор готов, важность: %s", change.analysis.importance)
        else:
            log.info("  разбора нет — в письме будет фрагмент текста")
    changes.sort(key=importance_rank)
    reason = getattr(analyzer, "disabled_reason", None)
    if changes and reason:
        # Например, закончились деньги на балансе: письмо уйдёт без разбора, но человек должен узнать почему.
        alerts.append(f"Агент-аналитик не работает: {reason}. В письме только фрагменты текста.")

    sent = False
    if changes or alerts:
        subject, text, html_body = render(changes, alerts)
        if dry_run:
            log.info("Пробный режим: письмо «%s» не отправляется, вот его текст:", subject)
            out(f"Тема: {subject}\n\n{text}")
        else:
            log.info("Отправляю письмо «%s»", subject)
            mailer(subject, text, html_body)  # при ошибке отправки исключение — состояние не сохраняется
            sent = True
            log.info("Письмо отправлено")
    else:
        log.info("Обновлений нет — письмо не отправляется")

    if not dry_run:
        saved = 0
        for r in results:
            records = r.silent + [c.record for c in r.changes]
            store.save_records(records)
            store.mark_success(r.source.id)
            saved += len(records)
        for sid in alerted_ids:
            store.mark_alerted(sid)
        log.info("Состояние сохранено (записей: %d)", saved)

    log.info("Готово: источников %d (сбоев %d), обновлений %d", len(sources), failed, len(changes))
    return RunReport(changes, alerts, failed, len(sources), sent)
