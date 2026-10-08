"""Сброс памяти: «почисти кэш, чтобы письмо пришло ещё раз»."""

import sys
from types import SimpleNamespace

import pytest

from sitewatch import cli
from sitewatch.models import DocRecord
from sitewatch.menu import run_menu
from sitewatch.store import Store
from tests.helpers import INDEX_URL, PATTERN, FakeFetcher, index_html, item_html, item_url


@pytest.fixture(autouse=True)
def reset_logging():
    yield
    cli.shutdown_logging()


def filled_store(path):
    store = Store(str(path))
    store.save_records([DocRecord("https://a/1", "a", "t", "h", "x"), DocRecord("https://a/2", "a", "t", "h", "x"),
                        DocRecord("https://b/1", "b", "t", "h", "x")])
    store.mark_success("a")
    store.mark_success("b")
    store.record_failure("b", "HTTP 403")
    return store


# --- хранилище ---


def test_store_reset_all_or_selected(tmp_path):
    store = filled_store(tmp_path / "s.db")
    assert store.known_sources() == {"a": 2, "b": 1}

    assert store.reset(["a"]) == (2, 1)
    assert store.known_sources() == {"b": 1} and store.source_state("a").baselined is False
    assert store.source_state("b").failures == 1  # соседний источник не тронут

    assert store.reset() == (1, 1)
    assert store.known_sources() == {}


# --- команда reset ---


def run_cli(tmp_path, *args):
    return cli.main(["--env", str(tmp_path / "нет.env"), "--log-file", "", *args])


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "data" / "state.db"
    path.parent.mkdir()
    monkeypatch.setenv("SITEWATCH_DB", str(path))
    return path


def test_reset_with_yes_clears_memory_and_keeps_a_backup(tmp_path, db, capsys):
    filled_store(db).close()
    assert run_cli(tmp_path, "reset", "--yes") == 0
    out = capsys.readouterr().out
    assert "документов 3" in out and "Резервная копия" in out and "initial_notify" in out

    store = Store(str(db))
    assert store.known_sources() == {}
    store.close()
    backup = Store(str(db.with_name("state.db.bak")))  # из копии всё можно вернуть, просто переименовав файл
    assert backup.known_sources() == {"a": 2, "b": 1}
    backup.close()


def test_reset_selected_source_only_and_unknown_source(tmp_path, db, capsys):
    filled_store(db).close()
    assert run_cli(tmp_path, "reset", "--yes", "--source", "a") == 0
    store = Store(str(db))
    assert store.known_sources() == {"b": 1}
    store.close()

    assert run_cli(tmp_path, "reset", "--yes", "--source", "нет-такого") == 2
    assert "нет-такого" in capsys.readouterr().out


def test_reset_without_database_or_with_empty_memory_is_harmless(tmp_path, db, capsys):
    assert run_cli(tmp_path, "reset", "--yes") == 0 and "сбрасывать нечего" in capsys.readouterr().out
    Store(str(db)).close()
    assert run_cli(tmp_path, "reset", "--yes") == 0 and "Память пуста" in capsys.readouterr().out


def test_reset_asks_before_forgetting(tmp_path, db, monkeypatch, capsys):
    filled_store(db).close()

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert run_cli(tmp_path, "reset") == 2 and "--yes" in capsys.readouterr().out  # без терминала молча не удаляем

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert run_cli(tmp_path, "reset") == 0 and "Отменено" in capsys.readouterr().out
    store = Store(str(db))
    assert store.known_sources() == {"a": 2, "b": 1}
    store.close()

    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert run_cli(tmp_path, "reset") == 0
    store = Store(str(db))
    assert store.known_sources() == {}
    store.close()


# --- главное: письмо приходит заново ---


def test_letter_comes_again_after_reset(tmp_path, db, monkeypatch, capsys):
    config = tmp_path / "sources.yaml"
    config.write_text(
        f"sources:\n  - id: cz-releases\n    name: Релизы\n    type: list\n    url: {INDEX_URL}\n    link_pattern: '{PATTERN}'\n",
        encoding="utf-8",
    )
    for key, value in {"SMTP_HOST": "smtp.example.com", "MAIL_FROM": "a@x.ru", "MAIL_TO": "b@x.ru"}.items():
        monkeypatch.setenv(key, value)
    pages = {INDEX_URL: index_html([20, 13]), item_url(20): item_html(20), item_url(13): item_html(13)}
    monkeypatch.setattr(cli, "Fetcher", lambda *a, **k: FakeFetcher(pages))
    sent = []
    monkeypatch.setattr(cli, "send_email", lambda settings, subject, text, html: sent.append(subject))

    def run():
        return run_cli(tmp_path, "run", "--config", str(config), "--no-llm")

    assert run() == 0 and len(sent) == 1  # первый запуск: письмо о самом свежем релизе
    assert run() == 0 and len(sent) == 1  # повторный: ничего нового, письма нет
    assert run_cli(tmp_path, "reset", "--yes") == 0
    assert run() == 0 and len(sent) == 2  # после сброса письмо приходит снова
    assert sent[0] == sent[1] and "Что нового в системе с 20.04.2026" in sent[1]


# --- меню ---


def scripted(*answers):
    it = iter(answers)
    return lambda prompt="": next(it)


def test_menu_reset_asks_confirmation_then_runs_with_yes():
    calls = []
    run_menu(lambda argv: calls.append(argv) or 0, ["--env", ".env"], "c.yaml", ".env", "",
             scripted("11", "n", "11", "y", "", "0"))
    assert calls == [["--env", ".env", "reset", "--yes"]]  # отказ ничего не запускает, согласие запускает без повторного вопроса
