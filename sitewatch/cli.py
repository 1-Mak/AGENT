"""Командная строка: sitewatch run | test-email."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from .analyze import Analyzer, NullAnalyzer
from .config import ConfigError, Settings, load_dotenv, load_sources
from .fetch import Fetcher
from .notify import send_email
from .pipeline import run
from .store import Store


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sitewatch", description="Мониторинг обновлений документации на сайтах")
    p.add_argument("--env", default=".env", help="файл с переменными окружения (по умолчанию .env)")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="проверить источники и отправить дайджест, если есть обновления")
    r.add_argument("--config", default="config/sources.yaml", help="файл с источниками")
    r.add_argument("--source", action="append", metavar="ID", help="проверять только этот источник (можно несколько раз)")
    r.add_argument("--dry-run", action="store_true", help="ничего не отправлять и не сохранять, письмо вывести на экран")
    r.add_argument("--no-llm", action="store_true", help="без агента-аналитика (в письме только фрагмент текста/diff)")

    sub.add_parser("test-email", help="отправить тестовое письмо, чтобы проверить настройки SMTP")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(args.env)
    settings = Settings.from_env()

    if args.command == "test-email":
        subject = "[Мониторинг] Тестовое письмо"
        text = f"Если вы читаете это письмо, SMTP настроен верно.\n{datetime.now():%d.%m.%Y %H:%M}\n"
        try:
            send_email(settings, subject, text, f"<p>{text}</p>")
        except Exception as e:  # noqa: BLE001 — показываем пользователю любую причину
            print(f"Не удалось отправить: {e}", file=sys.stderr)
            return 1
        print(f"Отправлено на: {', '.join(settings.mail_to)}")
        return 0

    try:
        sources = load_sources(args.config)
    except ConfigError as e:
        print(f"Ошибка конфигурации: {e}", file=sys.stderr)
        return 2
    if args.source:
        unknown = set(args.source) - {s.id for s in sources}
        if unknown:
            print(f"Нет таких источников в конфиге: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2
        sources = [s for s in sources if s.id in args.source]

    if not args.dry_run and settings.missing_mail_settings():
        print(
            "Не заданы настройки почты: " + ", ".join(settings.missing_mail_settings())
            + ". Заполните .env или запустите с --dry-run.",
            file=sys.stderr,
        )
        return 2

    analyzer = NullAnalyzer() if args.no_llm else Analyzer(settings.model, settings.max_doc_chars)
    store = Store(settings.db_path)
    fetcher = Fetcher(settings.user_agent, settings.request_timeout, settings.request_delay)
    try:
        report = run(
            sources,
            fetcher=fetcher,
            store=store,
            analyzer=analyzer,
            mailer=lambda subject, text, html_body: send_email(settings, subject, text, html_body),
            settings=settings,
            dry_run=args.dry_run,
        )
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("Запуск завершился ошибкой")
        return 1
    finally:
        store.close()

    # Если не удалось проверить вообще ни один источник — мониторинг «слепой», сообщаем это кодом возврата.
    return 1 if report.total_sources and report.failed_sources == report.total_sources else 0
