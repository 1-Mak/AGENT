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
from typing import Callable, Protocol, Sequence

from .config import Settings
from .extract import (
    ExtractError,
    extract_document,
    extract_links,
    extract_pdf_links,
    extract_pdf_pages,
    extract_text_file,
    is_file_url,
    is_pdf_url,
    local_path,
    parse_sitemap,
    parse_telegram,
)
from .fetch import FetchError
from .locate import ground_places
from .models import Change, DocRecord, Scope, Source
from .notify import render, sort_key
from .robots import GuardedFetcher, RobotsBlocked, RobotsPolicy
from .signals import MonitorResult, Signal
from .store import Store

log = logging.getLogger(__name__)

MAX_CHILD_SITEMAPS = 20


class SourceError(Exception):
    """Источник не удалось проверить (нет ссылок, сломалась вёрстка, блокировка и т.п.)."""


class PageFetcher(Protocol):
    def get(self, url: str) -> str: ...

    def get_bytes(self, url: str) -> bytes: ...


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
    filtered: list[Change] = field(default_factory=list)  # не относятся к теме читателя, в письмо не вошли
    signals: list[Signal] = field(default_factory=list)  # пробы API и счётчики журнала


# --- обнаружение документов ---


def discover(source: Source, fetcher: PageFetcher) -> list[str]:
    """Адреса документов источника, от новых к старым (насколько это известно из страницы)."""
    if source.type == "list":
        return extract_links(fetcher.get(source.url), source.url, source.link_pattern)

    # sitemap
    rx = re.compile(source.link_pattern)
    try:
        is_index, entries = parse_sitemap(fetcher.get(source.url))
        if is_index:
            children = [loc for loc, _ in entries][:MAX_CHILD_SITEMAPS]
            entries = []
            for child in children:
                _, child_entries = parse_sitemap(fetcher.get(child))
                entries.extend(child_entries)
    except ExtractError as e:
        raise SourceError(f"{source.url}: {e}") from e
    entries = [(loc, mod) for loc, mod in entries if rx.search(loc)]
    if entries and all(mod for _, mod in entries):
        entries.sort(key=lambda e: e[1], reverse=True)  # свежие первыми
    return list(dict.fromkeys(loc for loc, _ in entries))


def _read_bytes(fetcher: PageFetcher, url: str) -> bytes:
    return local_path(url).read_bytes() if is_file_url(url) else fetcher.get_bytes(url)


def _read_text(fetcher: PageFetcher, url: str) -> str:
    if is_file_url(url):
        return local_path(url).read_bytes().decode("utf-8", errors="replace")
    return fetcher.get(url)


def fetch_record(
    source: Source,
    url: str,
    fetcher: PageFetcher,
    *,
    as_pdf: bool = False,
    pdf_links: list[str] | None = None,
) -> DocRecord:
    """Скачивает документ и приводит его к тексту: PDF — через разбор файла, text — как есть, остальное — как HTML.
    Адреса file:// читаются с диска (удобно для собственной копии спецификации).
    as_pdf — читать как PDF, даже если в адресе нет .pdf. pdf_links — сюда добавляются ссылки на PDF со страницы."""
    line_pages = None
    try:
        if as_pdf or source.type == "pdf" or is_pdf_url(url):
            title, text, line_pages = extract_pdf_pages(_read_bytes(fetcher, url), url, source.ignore_regex)
        elif source.type == "text":
            title, text = extract_text_file(_read_text(fetcher, url), url)
        else:
            page = _read_text(fetcher, url)
            title, text = extract_document(page, url, source.content_selector, source.ignore_regex)
            if pdf_links is not None:
                pdf_links += extract_pdf_links(page, url, source.content_selector, source.pdf_pattern)
    except OSError as e:  # нет файла, нет прав
        raise SourceError(f"{url}: не удалось прочитать файл ({e})") from e
    except ExtractError as e:
        raise SourceError(f"{url}: {e}") from e
    if not text.strip():
        raise SourceError(f"{url}: на странице не найден текст (вёрстка изменилась или антибот-заглушка?)")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    log.info("  извлечено: «%s», %d символов", title, len(text))
    log.debug("  хэш текста: %s", digest[:12])
    return DocRecord(url, source.id, title, digest, text, line_pages)


def make_diff(old: str, new: str) -> str:
    return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), "было", "стало", lineterm="", n=2))


def _classify(res: SourceResult, store: Store, record: DocRecord, found_on: str = "", quiet: bool = False) -> None:
    """quiet — запомнить молча, если документ ещё неизвестен (PDF со страницы, которая сама пока только запоминается)."""
    old = store.get_document(record.url)
    if old is None:
        if quiet or (res.baseline and res.source.initial_notify == 0):
            log.info("  первый снимок страницы: запоминаю молча")
            res.silent.append(record)
        else:
            log.info("  НОВЫЙ документ -> войдёт в письмо")
            res.changes.append(Change(res.source, record, "new", found_on=found_on))
    elif old.content_hash == "":
        log.info("  первый снимок текста (адрес был известен): запоминаю молча")
        res.silent.append(record)  # URL был известен без текста — это первый снимок, не изменение
    elif old.content_hash != record.content_hash:
        log.info("  ИЗМЕНЁН -> войдёт в письмо")
        res.changes.append(Change(res.source, record, "updated", diff=make_diff(old.text, record.text), found_on=found_on))
    else:
        log.info("  без изменений")


def _check_page(
    res: SourceResult, source: Source, url: str, fetcher: PageFetcher, store: Store, seen_pdfs: set[str]
) -> None:
    """Страница (или файл) -> документ; при follow_pdf ещё и PDF, на которые она ссылается, каждый как отдельный документ."""
    links: list[str] | None = [] if source.follow_pdf else None
    silent_before = len(res.silent)
    _classify(res, store, fetch_record(source, url, fetcher, pdf_links=links))
    page_is_only_remembered = len(res.silent) > silent_before  # первый снимок страницы: её файлы тоже архив, а не новость
    if not links:
        return
    fresh = [u for u in links if u not in seen_pdfs]
    log.info("  PDF-ссылок на странице: %d", len(fresh))
    blocked: list[str] = []
    for pdf_url in fresh[: source.max_pdf_links]:
        seen_pdfs.add(pdf_url)
        log.info("  PDF %s", pdf_url)
        try:
            _classify(res, store, fetch_record(source, pdf_url, fetcher, as_pdf=True), found_on=url, quiet=page_is_only_remembered)
        except RobotsBlocked:
            blocked.append(pdf_url)
        except (FetchError, SourceError) as e:  # один плохой файл не должен ронять проверку страницы
            log.warning("[%s] пропускаю PDF %s: %s", source.id, pdf_url, e)
    if blocked:
        # одной строкой на страницу: подробности про robots.txt уже есть в README, а семь одинаковых предупреждений тонут в журнале
        log.warning(
            "[%s] robots.txt сайта запрещает загрузку PDF (%d на странице %s), они пропущены. Если вы вправе их забирать: "
            "robots: warn у этого источника в sources.yaml", source.id, len(blocked), url,
        )
    if len(fresh) > source.max_pdf_links:
        log.warning(
            "[%s] на странице %d PDF-ссылок, обрабатываю первые %d (max_pdf_links)", source.id, len(fresh), source.max_pdf_links
        )


def _check_telegram(source: Source, fetcher: PageFetcher, store: Store, res: SourceResult) -> SourceResult:
    """Канал целиком читается с одной страницы t.me/s/<канал>: каждое сообщение — отдельный документ."""
    messages = parse_telegram(_read_text(fetcher, source.url))
    if not messages:
        raise SourceError(
            f"{source.url}: не найдено ни одного сообщения с текстом — канал закрыт, адрес без /s/ "
            "(нужен вид https://t.me/s/имя_канала) или Telegram отдал заглушку"
        )
    records = [DocRecord(url, source.id, title, hashlib.sha256(text.encode("utf-8")).hexdigest(), text) for url, title, text in messages]
    known = store.known_urls(source.id)
    log.info("[%s] сообщений на странице: %d, из них новых: %d", source.id, len(records), sum(r.url not in known for r in records))

    if res.baseline:
        log.info("[%s] первый запуск: в письмо возьму самых свежих: %d, остальные запомню молча", source.id, source.initial_notify)
        for index, record in enumerate(records):
            if index < source.initial_notify:
                res.changes.append(Change(source, record, "new"))
            else:
                res.silent.append(record)
        return res

    fresh = [r for r in records if r.url not in known][: source.max_new_per_run]
    for record in fresh:
        log.info("[%s] %s: НОВОЕ сообщение -> войдёт в письмо", source.id, record.title)
        res.changes.append(Change(source, record, "new"))
    for record in records[: source.recheck_latest]:
        if record.url in known:
            _classify(res, store, record)  # правки уже опубликованных сообщений
    return res


def check_source(source: Source, fetcher: PageFetcher, store: Store) -> SourceResult:
    baseline = not store.source_state(source.id).baselined
    res = SourceResult(source, baseline=baseline)
    log.info("[%s] проверяю %s (тип: %s)", source.id, source.url, source.type)

    seen_pdfs: set[str] = set()
    if source.type in ("page", "pdf", "text"):
        _check_page(res, source, source.url, fetcher, store, seen_pdfs)
        return res

    if source.type == "telegram":
        return _check_telegram(source, fetcher, store, res)

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
            _check_page(res, source, url, fetcher, store, seen_pdfs)
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
    scope: Scope | None = None,
    monitors: Sequence[tuple[str, Callable[[], MonitorResult]]] = (),
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

    robots = RobotsPolicy(fetcher)
    for source in sources:
        mode = source.robots or settings.robots_mode
        source_fetcher = GuardedFetcher(fetcher, robots, mode) if mode in ("respect", "warn") else fetcher
        try:
            results.append(check_source(source, source_fetcher, store))
            continue
        except (SourceError, FetchError) as e:
            reason = str(e)
            log.error("[%s] сбой проверки: %s", source.id, reason)
        except Exception as e:  # noqa: BLE001 — непредвиденная ошибка разбора одного источника не должна останавливать остальные
            reason = f"непредвиденная ошибка разбора ({type(e).__name__}: {e})"
            log.exception("[%s] сбой проверки: %s", source.id, reason)
        failed += 1
        if dry_run:
            continue
        count = store.record_failure(source.id, reason)
        log.error("[%s] сбоев подряд: %d (письмо-алерт при %d)", source.id, count, settings.failure_alert_threshold)
        if count >= settings.failure_alert_threshold and not store.source_state(source.id).alerted:
            alerts.append(f"«{source.name}»: {count} запусков подряд не удаётся проверить. Причина: {reason}")
            alerted_ids.append(source.id)

    changes = [c for r in results for c in r.changes]
    for change in changes:
        log.info("Агент разбирает: «%s» (%s)", change.record.title, "новый" if change.kind == "new" else "изменён")
        change.analysis = analyzer.analyze(change)
        if change.analysis:
            ground_places(change)  # проверить цитаты агента по тексту и построить ссылки на места
            checked = sum(p.verified for p in change.analysis.places)
            log.info("  разбор готов, важность: %s, мест в документе: %d (цитата подтверждена у %d)",
                     change.analysis.importance, len(change.analysis.places), checked)
        else:
            log.info("  разбора нет — в письме будет фрагмент текста")

    scope = scope or Scope()
    filtered: list[Change] = []
    if scope.interests:
        kept = []
        for change in changes:
            a = change.analysis
            irrelevant = a is not None and (a.relevance == "не относится" or (a.relevance == "общее" and not scope.include_general))
            if irrelevant:
                log.info("«%s»: не относится к теме — в письмо не включаю (%s)", change.record.title, a.relevance_note or "без пояснения")
                filtered.append(change)
            else:
                kept.append(change)
        changes = kept

    changes.sort(key=sort_key)

    signals: list[Signal] = []
    commits: list[Callable[[], None]] = []
    for name, monitor in monitors:
        try:
            result = monitor()
        except Exception as e:  # noqa: BLE001 — сбой контроля API не должен лишать письма об обновлениях документов
            log.exception("%s: проверка не удалась", name)
            alerts.append(f"{name}: проверка не удалась ({type(e).__name__}: {e})")
            continue
        signals += result.signals
        commits.append(result.commit)
        for note in result.notes:
            log.info("%s: %s", name, note)
    reason = getattr(analyzer, "disabled_reason", None)
    if changes and reason:
        # Например, закончились деньги на балансе: письмо уйдёт без разбора, но человек должен узнать почему.
        alerts.append(f"Агент-аналитик не работает: {reason}. В письме только фрагменты текста.")

    sent = False
    if changes or alerts or signals:
        subject, text, html_body = render(changes, alerts, scope=scope, filtered=filtered, signals=signals)
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
        for commit in commits:
            commit()
        log.info("Состояние сохранено (записей: %d)", saved)

    log.info(
        "Готово: источников %d (сбоев %d), обновлений в письме %d, отфильтровано по теме %d, сигналов API %d",
        len(sources), failed, len(changes), len(filtered), len(signals),
    )
    return RunReport(changes, alerts, failed, len(sources), sent, filtered, signals)
