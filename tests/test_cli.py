"""CLI: журнал, меню, загрузка .env и сквозной запуск через main()."""

import logging
import logging.handlers
import re
from pathlib import Path

import pytest

from sitewatch import cli
from sitewatch.config import Settings, load_dotenv
from sitewatch.menu import run_menu
from sitewatch.store import Store
from tests.helpers import INDEX_URL, PATTERN, FakeFetcher, index_html, item_html, item_url

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def reset_logging():
    yield
    cli.shutdown_logging()


# --- журнал ---


def test_logging_goes_to_console_and_rotating_file(tmp_path, capsys):
    log_file = tmp_path / "logs" / "sitewatch.log"
    cli.setup_logging(False, str(log_file))
    logging.getLogger("sitewatch.demo").info("проверка журнала: привет")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "проверка журнала: привет" in log_file.read_text(encoding="utf-8")
    assert "проверка журнала: привет" in capsys.readouterr().err


def test_verbose_enables_debug_and_empty_log_file_disables_file(tmp_path):
    cli.setup_logging(True, "")
    assert logging.getLogger().level == logging.DEBUG
    # у pytest есть собственный FileHandler, поэтому проверяем именно наш файловый обработчик с ротацией
    assert not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in logging.getLogger().handlers)
    cli.setup_logging(False, "")
    assert logging.getLogger().level == logging.INFO


# --- .env ---


def test_dotenv_edits_are_picked_up_on_reload_but_real_env_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setenv("MAIL_FROM", "real@example.com")
    env.write_text("LLM_MODEL=deepseek-flash\nMAIL_FROM=file@example.com\n", encoding="utf-8")
    load_dotenv(env)
    assert Settings.from_env().llm_model == "deepseek-flash"

    env.write_text("LLM_MODEL=deepseek-v4-pro\nMAIL_FROM=file@example.com\n", encoding="utf-8")
    load_dotenv(env)  # как после правки в Блокноте из меню
    s = Settings.from_env()
    assert s.llm_model == "deepseek-v4-pro" and s.mail_from == "real@example.com"


def test_dotenv_with_bom_from_windows_notepad(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes("﻿LLM_MODEL=deepseek-flash\r\nMAIL_TO=a@x.ru\r\n".encode("utf-8"))
    load_dotenv(env)
    s = Settings.from_env()
    assert s.llm_model == "deepseek-flash" and s.mail_to == ("a@x.ru",)


# --- меню ---


def scripted(*answers):
    it = iter(answers)
    return lambda prompt="": next(it)


def test_menu_runs_selected_actions_with_base_args_and_config(capsys):
    calls = []

    def fake_cli(argv):
        calls.append(argv)
        return 0

    run_menu(fake_cli, ["--env", ".env"], "my.yaml", ".env", "logs/x.log",
             scripted("1", "", "5", "", "3", "", "0"))
    assert calls == [
        ["--env", ".env", "doctor", "--config", "my.yaml"],
        ["--env", ".env", "run", "--dry-run", "--config", "my.yaml"],
        ["--env", ".env", "test-llm"],  # test-llm не принимает --config
    ]
    assert "logs/x.log" in capsys.readouterr().out


def test_menu_asks_confirmation_before_real_run():
    calls = []
    run_menu(lambda argv: calls.append(argv) or 0, [], "c.yaml", ".env", "", scripted("7", "n", "7", "y", "", "0"))
    assert calls == [["run", "--config", "c.yaml"]]  # отказ не запускает, согласие запускает


def test_menu_survives_command_crash_and_bad_input(capsys):
    def boom(argv):
        raise RuntimeError("что-то сломалось")

    rc = run_menu(boom, [], "c.yaml", ".env", "", scripted("abc", "1", "", "0"))
    captured = capsys.readouterr()
    assert rc == 0  # меню дожило до выбора «0»
    assert "Нет такого пункта" in captured.out and "есть проблемы" in captured.out
    assert "что-то сломалось" in captured.err  # трассировка показана, а не проглочена


def test_menu_exits_on_eof():
    def eof(prompt=""):
        raise EOFError

    assert run_menu(lambda argv: 0, [], "c.yaml", ".env", "", eof) == 0


# --- сквозной запуск через main() ---


def test_main_dry_run_prints_letter_and_leaves_database_empty(tmp_path, monkeypatch, capsys):
    config = tmp_path / "sources.yaml"
    config.write_text(
        f"sources:\n  - id: cz-releases\n    name: Релизы\n    type: list\n    url: {INDEX_URL}\n    link_pattern: '{PATTERN}'\n",
        encoding="utf-8",
    )
    db = tmp_path / "state.db"
    monkeypatch.setenv("SITEWATCH_DB", str(db))
    pages = {INDEX_URL: index_html([20, 13]), item_url(20): item_html(20)}
    monkeypatch.setattr(cli, "Fetcher", lambda *a, **k: FakeFetcher(pages))

    rc = cli.main(["--env", str(tmp_path / "нет.env"), "--log-file", "", "run", "--config", str(config), "--dry-run", "--no-llm"])
    out = capsys.readouterr()
    assert rc == 0
    assert "Что нового в системе с 20.04.2026" in out.out  # письмо выведено на экран
    assert "найдено документов 2" in out.err and "ПРОБНЫЙ" in out.err  # ход работы виден в консоли
    store = Store(str(db))
    assert store.known_urls("cz-releases") == set()
    store.close()


def test_main_failed_send_explains_and_keeps_state_unsaved(tmp_path, monkeypatch, capsys):
    from sitewatch.notify import MailError

    config = tmp_path / "sources.yaml"
    config.write_text(
        f"sources:\n  - id: cz-releases\n    type: list\n    url: {INDEX_URL}\n    link_pattern: '{PATTERN}'\n",
        encoding="utf-8",
    )
    db = tmp_path / "state.db"
    for key, value in {"SITEWATCH_DB": str(db), "SMTP_HOST": "smtp.example.com", "MAIL_FROM": "a@x.ru", "MAIL_TO": "b@x.ru"}.items():
        monkeypatch.setenv(key, value)
    pages = {INDEX_URL: index_html([20, 13]), item_url(20): item_html(20)}
    monkeypatch.setattr(cli, "Fetcher", lambda *a, **k: FakeFetcher(pages))

    def broken_send(*args, **kwargs):
        raise MailError("сервер smtp.example.com:465 отклонил логин или пароль")

    monkeypatch.setattr(cli, "send_email", broken_send)
    rc = cli.main(["--env", str(tmp_path / "нет.env"), "--log-file", "", "run", "--config", str(config), "--no-llm"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "отклонил логин или пароль" in err and "Состояние НЕ сохранено" in err
    assert "Traceback" not in err  # ожидаемая ошибка — без страшной трассировки
    store = Store(str(db))
    assert store.known_urls("cz-releases") == set()
    store.close()


def test_main_rejects_unknown_source_and_missing_config(tmp_path, capsys):
    base = ["--env", str(tmp_path / "нет.env"), "--log-file", ""]
    assert cli.main(base + ["check-sources", "--config", str(tmp_path / "нет.yaml")]) == 2
    assert "не найден" in capsys.readouterr().err


def test_main_run_without_mail_settings_explains_what_to_do(tmp_path, monkeypatch, capsys):
    for key in ("SMTP_HOST", "MAIL_FROM", "MAIL_TO", "SMTP_USER"):
        monkeypatch.delenv(key, raising=False)
    config = tmp_path / "s.yaml"
    config.write_text("sources:\n  - {id: a, type: page, url: 'https://x.ru/'}\n", encoding="utf-8")
    rc = cli.main(["--env", str(tmp_path / "нет.env"), "--log-file", "", "run", "--config", str(config)])
    assert rc == 2 and "--dry-run" in capsys.readouterr().err


# --- батник ---


def test_launcher_is_ascii_crlf_and_all_goto_targets_exist():
    raw = (ROOT / "run.bat").read_bytes()
    assert all(b < 128 for b in raw), "в батнике только ASCII — русский текст живёт в Python"
    assert b"\n" not in raw.replace(b"\r\n", b""), "строки батника должны заканчиваться CRLF"

    text = raw.decode("ascii")
    labels = set(re.findall(r"^:(\w+)", text, flags=re.MULTILINE))
    targets = set(re.findall(r"\bgoto\s+:?(\w+)", text, flags=re.IGNORECASE)) - {"eof"}
    assert targets <= labels, f"goto на несуществующие метки: {targets - labels}"
    assert labels <= targets, f"метки, на которые никто не переходит: {labels - targets}"


def test_launcher_has_the_essential_steps():
    text = (ROOT / "run.bat").read_text(encoding="ascii")
    for needle in ("py -3", ".venv", "pip install", "-e .", ".env.example", "-m sitewatch menu", "-m sitewatch run", "chcp 65001", "PYTHONUTF8"):
        assert needle in text, needle
    # режим планировщика не должен ждать нажатия клавиши
    auto_block = text.split(":auto_run", 1)[1].split(":venv_failed", 1)[0]
    assert "pause" not in auto_block.lower()
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("*.bat") and "-text" in line for line in attributes), "git не должен менять CRLF в run.bat"
