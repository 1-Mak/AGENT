"""Диагностика: doctor (настройки, состояние, сеть) и check-sources (что видит парсер на сайтах)."""

from __future__ import annotations

import os
import platform
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__, ui
from .config import ConfigError, Settings, load_scope, load_sources
from .extract import extract_document, extract_links, parse_telegram
from .fetch import FetchError
from .logwatch import expand_paths, load_logwatch
from .models import Source
from .pipeline import PageFetcher, SourceError, discover, fetch_record
from .probes import load_probes, prepare
from .robots import GuardedFetcher, RobotsPolicy
from .store import Store

PREVIEW_LINES = 15
SAMPLE_LINKS = 5


def _tcp_check(host: str, port: int, timeout: float = 5.0) -> tuple[bool, str]:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"соединение установлено за {(time.monotonic() - started) * 1000:.0f} мс"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


def _host_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    return parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)


def _doctor_monitors(probes_path: Path, logs_path: Path) -> int:
    """Контрольные вызовы API и журналы интеграции: файлы есть, переменные заданы, журналы находятся."""
    fails = 0
    ui.title("Контрольные вызовы API")
    if not probes_path.is_file():
        ui.info(f"{probes_path} нет — контрольных вызовов API не будет (шаблон: config/probes.yaml)")
    else:
        try:
            config = load_probes(probes_path)
            ready = 0
            for probe in config.probes:
                prepared = prepare(probe, os.environ)
                if isinstance(prepared, str):
                    ui.warn(f"{probe.id}: {prepared}")
                else:
                    ready += 1
                    note = "" if prepared.mode == "full" else " (без токена)"
                    ui.ok(f"{probe.id}: {probe.method} {prepared.host}{note}")
            ui.info(f"проб в файле {len(config.probes)}, готово к запуску {ready}")
            if config.token_command:
                ui.info("токен получается командой из probes.yaml (token.command)")
            elif not os.environ.get(config.token_var):
                ui.info(f"{config.token_var} не задан: методы с авторизацией проверяются без токена (видно статус и текст ошибки)")
        except ConfigError as e:
            fails += 1
            ui.fail(str(e))

    ui.title("Журнал интеграции")
    if not logs_path.is_file():
        ui.info(f"{logs_path} нет — журнал читаться не будет (шаблон: config/logwatch.yaml)")
    else:
        try:
            for spec in load_logwatch(logs_path):
                files = expand_paths(spec)
                if files:
                    ui.ok(f"{spec.id}: найдено файлов {len(files)}, например {files[0]}")
                else:
                    ui.warn(f"{spec.id}: по путям {', '.join(spec.paths)} файлов нет — проверьте пути в logwatch.yaml")
                ui.info("ищем: " + ", ".join(s.name for s in spec.signatures))
        except ConfigError as e:
            fails += 1
            ui.fail(str(e))
    return fails


def doctor(
    settings: Settings,
    config_path: str,
    env_path: str,
    online: bool = True,
    monitor_paths: tuple[Path, Path] | None = None,
) -> int:
    """Проверяет всё, что нужно для запуска. Возвращает 1, если есть критичные проблемы."""
    fails = 0

    ui.title("Окружение")
    ui.ok(f"Python {platform.python_version()} ({sys.executable})")
    ui.ok(f"sitewatch {__version__}; рабочая папка: {Path.cwd()}")
    env_file = Path(env_path)
    if env_file.is_file():
        ui.ok(f"файл настроек: {env_file.resolve()}")
    else:
        ui.warn(f"файла настроек {env_path} нет — используются только переменные окружения")

    ui.title("Почта (SMTP)")
    missing = settings.missing_mail_settings()
    if missing:
        fails += 1
        ui.fail("не заданы: " + ", ".join(missing) + " — без них письмо не отправить (запуск возможен только с --dry-run)")
    else:
        ui.ok(f"сервер: {settings.smtp_host}:{settings.smtp_port} (шифрование: {settings.smtp_security or 'по порту'})")
        ui.ok(f"от кого: {settings.mail_from}; кому: {', '.join(settings.mail_to)}")
    if settings.smtp_user:
        (ui.ok if settings.smtp_password else ui.warn)(
            f"логин: {settings.smtp_user}; пароль: {ui.mask(settings.smtp_password)}"
        )
    else:
        ui.warn("SMTP_USER не задан — отправка без авторизации (редкий случай)")

    ui.title("Агент-аналитик (DeepSeek)")
    if settings.llm_api_key:
        ui.ok(f"ключ: {ui.mask(settings.llm_api_key)}")
    else:
        ui.warn("DEEPSEEK_API_KEY не задан — письма будут без разбора, только фрагменты текста")
    ui.ok(f"модель: {settings.llm_model}; размышления: {'вкл' if settings.llm_thinking else 'выкл'}; {settings.llm_base_url}")

    ui.title("Тема писем (фильтр)")
    try:
        scope = load_scope(config_path)
        if scope.interests:
            ui.ok(f"в письма попадает только: {scope.interests}")
            ui.info("общие изменения (API, авторизация, форматы): " + ("берём" if scope.include_general else "не берём"))
        else:
            ui.warn("фильтра по теме нет (блок scope.interests в конфиге пуст) — присылается всё найденное")
        if scope.glossary:
            filled = sum(1 for e in scope.glossary if e.where)
            ui.ok(f"таблица «что менять»: записей {len(scope.glossary)}, с указанием места правки {filled}")
            if filled < len(scope.glossary):
                ui.warn("у части записей не заполнено where: в письме будет только название, без места правки")
        else:
            ui.info("таблицы «что менять» нет (scope.glossary) — письмо ответит на «что изменилось», но не на «где править»")
    except ConfigError as e:
        fails += 1
        ui.fail(str(e))

    ui.title("Источники")
    sources: list[Source] = []
    try:
        sources = load_sources(config_path)
        enabled = [s for s in sources if s.enabled]
        ui.ok(f"{config_path}: источников {len(sources)}, включено {len(enabled)}")
        for s in sources:
            mark = "" if s.enabled else "  [ВЫКЛЮЧЕН: enabled: false]"
            pdf = "  +PDF по ссылкам" if s.follow_pdf else ""
            ui.info(f"- {s.id} [{s.type}] {s.url}{pdf}{mark}")
            if s.link_pattern:
                ui.info(f"    шаблон ссылок: {s.link_pattern}")
        if len(enabled) < len(sources):
            ui.warn("Есть выключенные источники (enabled: false): они не участвуют в запуске. Включите или удалите их.")
    except ConfigError as e:
        fails += 1
        ui.fail(str(e))

    if monitor_paths:
        fails += _doctor_monitors(*monitor_paths)

    ui.title("Состояние (база данных)")
    db_path = Path(settings.db_path)
    if not db_path.exists():
        ui.ok(f"{db_path}: базы ещё нет — это нормально до первого боевого запуска")
    else:
        store = Store(str(db_path))
        try:
            ui.ok(f"{db_path.resolve()} ({db_path.stat().st_size // 1024} КБ)")
            for s in sources:
                d = store.describe_source(s.id)
                state = "первый запуск ещё не завершён" if not d["baselined"] else f"известно документов: {d['documents']}"
                ui.info(f"- {s.id}: {state}; последний успех: {d['last_ok'] or '—'}")
                if d["failures"]:
                    ui.warn(f"{s.id}: сбоев подряд {d['failures']}; последняя ошибка: {d['last_error']}")
        finally:
            store.close()

    if online:
        ui.title("Сеть (проверка соединения)")
        if any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")):
            ui.warn("задан прокси в переменных окружения: проверка ниже идёт напрямую и может не совпадать с реальной работой")
        ui.info(f"robots.txt: режим {settings.robots_mode} (respect — не ходить туда, где запрещено; warn — только предупреждать; off)")
        targets: dict[str, tuple[str, int]] = {}
        for s in sources:
            host, port = _host_port(s.url)
            if s.enabled and host:  # file:// и выключенные источники по сети не проверяем
                targets[f"источник {s.id}"] = (host, port)
        if settings.llm_api_key:
            targets["DeepSeek API"] = _host_port(settings.llm_base_url)
        if settings.smtp_host:
            targets["почтовый сервер"] = (settings.smtp_host, settings.smtp_port)
        if not targets:
            ui.warn("проверять нечего")
        for label, (host, port) in targets.items():
            reachable, details = _tcp_check(host, port)
            if reachable:
                ui.ok(f"{label}: {host}:{port} — {details}")
            else:
                fails += 1
                ui.fail(f"{label}: {host}:{port} — {details}")
        ui.info("Это проверка только соединения. Что именно отдаёт сайт — смотрите в «Проверке источников».")

    print()
    if fails:
        ui.fail(f"Найдено проблем: {fails}. Исправьте их и запустите проверку снова.")
        return 1
    ui.ok("Критичных проблем нет.")
    return 0


# --- check-sources ---


def _preview(source: Source, url: str, fetcher: PageFetcher) -> None:
    pdf_links: list[str] | None = [] if source.follow_pdf else None
    record = fetch_record(source, url, fetcher, pdf_links=pdf_links)
    lines = record.text.splitlines()
    ui.ok(f"документ: {url}")
    ui.info(f"заголовок: {record.title}")
    ui.info(f"текст: {len(record.text)} символов, {len(lines)} строк")
    if len(record.text) < 200:
        ui.warn("текста очень мало: возможно, это заглушка/антибот или нужен content_selector в config/sources.yaml")
    ui.info("начало текста, который получит агент:")
    for line in lines[:PREVIEW_LINES]:
        ui.info("  | " + (line if len(line) <= 110 else line[:107] + "..."))
    if len(lines) > PREVIEW_LINES:
        ui.info(f"  | ... ещё строк: {len(lines) - PREVIEW_LINES}")
    ui.info("Если здесь попало меню сайта или баннеры — задайте content_selector; счётчики — ignore_regex.")
    if pdf_links is not None:
        if pdf_links:
            ui.ok(f"PDF-ссылок на странице: {len(pdf_links)} (будут прочитаны первые {source.max_pdf_links}, каждый как отдельный документ)")
            _show_links(pdf_links)
        else:
            ui.warn("follow_pdf включён, но PDF-ссылок в основном блоке страницы нет (проверьте content_selector и pdf_pattern)")


def _show_links(links: list[str]) -> None:
    for link in links[:SAMPLE_LINKS]:
        ui.info(f"  {link}")
    if len(links) > SAMPLE_LINKS:
        ui.info(f"  ... и ещё {len(links) - SAMPLE_LINKS}")


def _report_robots(policy: RobotsPolicy, source: Source, mode: str) -> bool:
    """Печатает вердикт robots.txt. True — в режиме respect загрузка запрещена и источник проверять нельзя."""
    verdict = policy.check(source.url)
    if verdict.allowed:
        ui.ok(f"robots.txt: загрузка разрешена. {verdict.note}".strip())
    elif mode == "respect":
        ui.fail("robots.txt сайта ЗАПРЕЩАЕТ автоматическую загрузку этого адреса; в режиме respect источник не будет работать")
        ui.info("Если вы вправе забирать эту страницу, разрешите явно: robots: warn (или off) у источника в sources.yaml.")
    else:
        ui.warn(f"robots.txt сайта запрещает загрузку этого адреса; режим {mode}: загружаю всё равно, решение за вами")
    if verdict.crawl_delay:
        ui.info(f"сайт просит паузу между запросами: {verdict.crawl_delay} с (в настройках SITEWATCH_REQUEST_DELAY)")
    return not verdict.allowed and mode == "respect"


def check_sources(sources: list[Source], fetcher: PageFetcher, robots_mode: str = "off") -> int:
    """Показывает, что видит парсер на каждом сайте. Базу не трогает, писем не шлёт."""
    fails = 0
    policy = RobotsPolicy(fetcher)
    for source in sources:
        ui.title(f"{source.name}  [{source.id}]" + ("" if source.enabled else "  (ВЫКЛЮЧЕН)"))
        ui.info(f"тип: {source.type}; адрес: {source.url}")
        mode = source.robots or robots_mode
        if mode in ("respect", "warn"):
            if _report_robots(policy, source, mode):
                fails += 1
                continue  # запрещено — страницу не запрашиваем, причина уже показана
            fetcher_for_source: PageFetcher = GuardedFetcher(fetcher, policy, mode)
        else:
            fetcher_for_source = fetcher
        fetcher_ = fetcher_for_source
        try:
            if source.type in ("page", "pdf", "text"):
                _preview(source, source.url, fetcher_)
            elif source.type == "telegram":
                messages = parse_telegram(fetcher_.get(source.url))
                if messages:
                    ui.ok(f"сообщений с текстом на странице: {len(messages)} (первое — самое свежее)")
                    for _, title, _ in messages[:3]:
                        ui.info(f"  {title}")
                    for line in messages[0][2].splitlines()[:PREVIEW_LINES]:
                        ui.info("  | " + (line if len(line) <= 110 else line[:107] + "..."))
                else:
                    fails += 1
                    ui.fail("сообщений не найдено: канал закрыт, адрес без /s/ (нужен https://t.me/s/имя_канала) или пришла заглушка")
            elif source.type == "list":
                page = fetcher_.get(source.url)
                page_title, _ = extract_document(page, source.url)
                ui.ok(f"страница списка загружена: «{page_title}», {len(page)} символов HTML")
                matches = extract_links(page, source.url, source.link_pattern)
                if matches:
                    ui.ok(f"ссылок по шаблону {source.link_pattern!r}: {len(matches)} (первая — самая свежая)")
                    _show_links(matches)
                    _preview(source, matches[0], fetcher_)
                else:
                    fails += 1
                    ui.fail(f"по шаблону {source.link_pattern!r} ничего не найдено")
                    everything = extract_links(page, source.url, ".")
                    ui.info(f"всего ссылок на странице: {len(everything)}; примеры:")
                    _show_links(everything)
                    ui.info("Что проверить: 1) не заглушка ли это (заголовок страницы выше); 2) подходит ли шаблон")
                    ui.info("к этим адресам; 3) если ссылок почти нет — список грузится скриптом, используйте type: sitemap.")
            else:  # sitemap
                urls = discover(source, fetcher_)
                if urls:
                    ui.ok(f"адресов в sitemap по шаблону {source.link_pattern!r}: {len(urls)}")
                    _show_links(urls)
                    _preview(source, urls[0], fetcher_)
                else:
                    fails += 1
                    ui.fail(f"в sitemap нет адресов по шаблону {source.link_pattern!r}")
        except (FetchError, SourceError) as e:
            fails += 1
            ui.fail(str(e))
        except Exception as e:  # noqa: BLE001 — диагностика не должна падать на кривой разметке или XML
            fails += 1
            ui.fail(f"неожиданная ошибка разбора: {type(e).__name__}: {e}")
    print()
    (ui.fail if fails else ui.ok)(f"Проблемных источников: {fails}" if fails else "Все источники читаются.")
    return 1 if fails else 0
