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
from .config import ConfigError, Settings, load_sources
from .extract import extract_document, extract_links
from .fetch import FetchError
from .models import Source
from .pipeline import PageFetcher, SourceError, discover, fetch_record
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


def doctor(settings: Settings, config_path: str, env_path: str, online: bool = True) -> int:
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

    ui.title("Источники")
    sources: list[Source] = []
    try:
        sources = load_sources(config_path)
        ui.ok(f"{config_path}: источников {len(sources)}")
        for s in sources:
            ui.info(f"- {s.id} [{s.type}] {s.url}")
            if s.link_pattern:
                ui.info(f"    шаблон ссылок: {s.link_pattern}")
    except ConfigError as e:
        fails += 1
        ui.fail(str(e))

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
        targets: dict[str, tuple[str, int]] = {}
        for s in sources:
            host, port = _host_port(s.url)
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
    record = fetch_record(source, url, fetcher)
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


def _show_links(links: list[str]) -> None:
    for link in links[:SAMPLE_LINKS]:
        ui.info(f"  {link}")
    if len(links) > SAMPLE_LINKS:
        ui.info(f"  ... и ещё {len(links) - SAMPLE_LINKS}")


def check_sources(sources: list[Source], fetcher: PageFetcher) -> int:
    """Показывает, что видит парсер на каждом сайте. Базу не трогает, писем не шлёт."""
    fails = 0
    for source in sources:
        ui.title(f"{source.name}  [{source.id}]")
        ui.info(f"тип: {source.type}; адрес: {source.url}")
        try:
            if source.type in ("page", "pdf"):
                _preview(source, source.url, fetcher)
            elif source.type == "list":
                page = fetcher.get(source.url)
                page_title, _ = extract_document(page, source.url)
                ui.ok(f"страница списка загружена: «{page_title}», {len(page)} символов HTML")
                matches = extract_links(page, source.url, source.link_pattern)
                if matches:
                    ui.ok(f"ссылок по шаблону {source.link_pattern!r}: {len(matches)} (первая — самая свежая)")
                    _show_links(matches)
                    _preview(source, matches[0], fetcher)
                else:
                    fails += 1
                    ui.fail(f"по шаблону {source.link_pattern!r} ничего не найдено")
                    everything = extract_links(page, source.url, ".")
                    ui.info(f"всего ссылок на странице: {len(everything)}; примеры:")
                    _show_links(everything)
                    ui.info("Что проверить: 1) не заглушка ли это (заголовок страницы выше); 2) подходит ли шаблон")
                    ui.info("к этим адресам; 3) если ссылок почти нет — список грузится скриптом, используйте type: sitemap.")
            else:  # sitemap
                urls = discover(source, fetcher)
                if urls:
                    ui.ok(f"адресов в sitemap по шаблону {source.link_pattern!r}: {len(urls)}")
                    _show_links(urls)
                    _preview(source, urls[0], fetcher)
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
