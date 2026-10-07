"""Командная строка sitewatch: run, doctor, check-sources, test-llm, test-email, menu."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .analyze import Analyzer, LLMError, NullAnalyzer, sample_change
from .config import ConfigError, Settings, load_dotenv, load_sources
from .doctor import check_sources, doctor
from .fetch import Fetcher
from .menu import run_menu
from .models import Source
from .notify import MailError, send_email
from .pipeline import run
from .store import Store

log = logging.getLogger(__name__)
DEFAULT_LOG_FILE = "logs/sitewatch.log"
_installed_handlers: list[logging.Handler] = []


def setup_console() -> None:
    """Печатаем в UTF-8 в любом случае: на старых кодовых страницах Windows русский текст иначе падает."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass  # подменённый поток (например, в тестах)


def setup_logging(verbose: bool = False, log_file: str = DEFAULT_LOG_FILE) -> None:
    """Журнал в консоль и в файл с ротацией. Повторный вызов заменяет прежние обработчики."""
    shutdown_logging()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    handlers: list[logging.Handler] = [console]
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
            handlers.append(file_handler)
        except OSError as e:
            print(f"Не удалось открыть файл журнала {log_file}: {e}", file=sys.stderr)
    for handler in handlers:
        root.addHandler(handler)
        _installed_handlers.append(handler)
    if not verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)


def shutdown_logging() -> None:
    root = logging.getLogger()
    for handler in _installed_handlers:
        root.removeHandler(handler)
        handler.close()
    _installed_handlers.clear()


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config/sources.yaml", help="файл с источниками")

    p = argparse.ArgumentParser(prog="sitewatch", description="Мониторинг обновлений документации на сайтах")
    p.add_argument("--env", default=".env", help="файл с переменными окружения (по умолчанию .env)")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный журнал (DEBUG), в том числе сетевые запросы")
    p.add_argument(
        "--log-file", default=DEFAULT_LOG_FILE, help=f"файл журнала (по умолчанию {DEFAULT_LOG_FILE}; пустая строка — не писать)"
    )
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", parents=[common], help="проверить источники и отправить дайджест, если есть обновления")
    r.add_argument("--source", action="append", metavar="ID", help="проверять только этот источник (можно несколько раз)")
    r.add_argument("--dry-run", action="store_true", help="ничего не отправлять и не сохранять, письмо вывести на экран")
    r.add_argument("--no-llm", action="store_true", help="без агента-аналитика (в письме только фрагмент текста/diff)")

    d = sub.add_parser("doctor", parents=[common], help="проверить настройки, состояние и соединения")
    d.add_argument("--offline", action="store_true", help="не проверять сетевые соединения")

    c = sub.add_parser("check-sources", parents=[common], help="показать, что видит парсер на сайтах (базу не трогает)")
    c.add_argument("--source", action="append", metavar="ID", help="только этот источник")

    sub.add_parser("test-email", help="отправить тестовое письмо, чтобы проверить настройки SMTP")
    sub.add_parser("test-llm", help="проверить ключ и модель DeepSeek на примере и показать расход токенов")
    sub.add_parser("menu", parents=[common], help="интерактивное меню")
    return p


def _test_llm(settings: Settings) -> int:
    if not settings.llm_api_key:
        print("Не задан DEEPSEEK_API_KEY (ключ создаётся на platform.deepseek.com).", file=sys.stderr)
        return 1
    analyzer = Analyzer.from_settings(settings)
    mode = "вкл" if settings.llm_thinking else "выкл"
    print(f"Модель: {settings.llm_model} ({settings.llm_base_url}), режим размышлений: {mode}")
    try:
        models = analyzer.list_models()
        print("Доступные модели:", ", ".join(models))
        if settings.llm_model not in models:
            print(f"ВНИМАНИЕ: модели {settings.llm_model!r} нет в списке — проверьте LLM_MODEL.", file=sys.stderr)
    except LLMError as e:
        print(f"Список моделей не получен ({e}), продолжаю проверку анализа.", file=sys.stderr)

    result = analyzer.analyze(sample_change())
    if result is None:
        print(f"Анализ не удался: {analyzer.disabled_reason or 'подробности в журнале'}", file=sys.stderr)
        return 1
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
    usage = analyzer.last_usage
    print(
        f"Токены: вход {usage.get('prompt_tokens')} "
        f"(из кэша {usage.get('prompt_cache_hit_tokens', 0)}), выход {usage.get('completion_tokens')}"
    )
    return 0


def _select_sources(config: str, wanted: list[str] | None) -> list[Source] | None:
    try:
        sources = load_sources(config)
    except ConfigError as e:
        print(f"Ошибка конфигурации: {e}", file=sys.stderr)
        return None
    if wanted:
        unknown = set(wanted) - {s.id for s in sources}
        if unknown:
            print(f"Нет таких источников в конфиге: {', '.join(sorted(unknown))}", file=sys.stderr)
            return None
        sources = [s for s in sources if s.id in wanted]
    return sources


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    setup_console()
    setup_logging(args.verbose, args.log_file)
    load_dotenv(args.env)
    settings = Settings.from_env()
    log.info("sitewatch %s, команда: %s, настройки: %s", __version__, args.command, Path(args.env).resolve())

    if args.command == "menu":
        base = ["--env", args.env, "--log-file", args.log_file] + (["-v"] if args.verbose else [])
        return run_menu(main, base, args.config, args.env, args.log_file)

    if args.command == "doctor":
        return doctor(settings, args.config, args.env, online=not args.offline)

    if args.command == "test-llm":
        return _test_llm(settings)

    if args.command == "test-email":
        subject = "[Мониторинг] Тестовое письмо"
        text = f"Если вы читаете это письмо, SMTP настроен верно.\n{datetime.now():%d.%m.%Y %H:%M}\n"
        log.info("Отправляю тестовое письмо через %s:%s", settings.smtp_host, settings.smtp_port)
        try:
            send_email(settings, subject, text, f"<p>{text}</p>")
        except MailError as e:
            log.error("Тестовое письмо не отправлено: %s", e)
            return 1
        print(f"Отправлено на: {', '.join(settings.mail_to)}")
        return 0

    sources = _select_sources(args.config, args.source)
    if sources is None:
        return 2
    fetcher = Fetcher(settings.user_agent, settings.request_timeout, settings.request_delay)

    if args.command == "check-sources":
        return check_sources(sources, fetcher)

    # run
    if not args.dry_run and settings.missing_mail_settings():
        print(
            "Не заданы настройки почты: " + ", ".join(settings.missing_mail_settings())
            + ". Заполните .env или запустите с --dry-run.",
            file=sys.stderr,
        )
        return 2

    if not args.no_llm and not settings.llm_api_key:
        log.warning("DEEPSEEK_API_KEY не задан — письма будут без разбора агентом")
    analyzer = NullAnalyzer() if args.no_llm else Analyzer.from_settings(settings)

    def mailer(subject: str, text: str, html_body: str) -> None:
        log.info("Отправляю на %s через %s:%s", ", ".join(settings.mail_to), settings.smtp_host, settings.smtp_port)
        send_email(settings, subject, text, html_body)

    store = Store(settings.db_path)
    try:
        report = run(
            sources,
            fetcher=fetcher,
            store=store,
            analyzer=analyzer,
            mailer=mailer,
            settings=settings,
            dry_run=args.dry_run,
        )
    except MailError as e:
        log.error("Письмо не отправлено: %s", e)
        log.error("Состояние НЕ сохранено: при следующем запуске изменения будут найдены заново, ничего не потеряно.")
        return 1
    except Exception:  # noqa: BLE001 — неожиданная ошибка: показываем трассировку для отладки
        log.exception("Запуск завершился неожиданной ошибкой")
        return 1
    finally:
        store.close()

    # Если не удалось проверить вообще ни один источник — мониторинг «слепой», сообщаем это кодом возврата.
    return 1 if report.total_sources and report.failed_sources == report.total_sources else 0
