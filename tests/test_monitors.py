"""Сигналы API и журнала в письме, запуск через run/CLI, doctor, reset, меню; краткий формат дайджеста."""

from datetime import date

import pytest

from sitewatch import cli
from sitewatch.doctor import _doctor_monitors
from sitewatch.menu import ACTIONS, run_menu
from sitewatch.models import GlossaryEntry, Scope
from sitewatch.notify import deadline_date, render, sort_key
from sitewatch.pipeline import run
from sitewatch.signals import MonitorResult, Signal
from sitewatch.store import Store
from tests.api_server import ApiServer
from tests.helpers import (
    INDEX_URL,
    PATTERN,
    FakeAnalyzer,
    FakeFetcher,
    Mailbox,
    index_html,
    item_html,
    item_url,
    make_analysis,
    make_change,
    make_settings,
    make_source,
    make_store,
)


def scripted(*answers):
    it = iter(answers)
    return lambda prompt="": next(it)


@pytest.fixture(autouse=True)
def reset_logging():
    yield
    cli.shutdown_logging()


@pytest.fixture
def api():
    server = ApiServer()
    yield server
    server.close()


def high(title="cises/info", message="HTTP 410 Gone: метод отключён"):
    return Signal("probe", "cises-info", title, "высокая", message)


# --- краткий формат письма ---


def test_digest_item_is_four_short_rows_what_where_how_deadline():
    scope = Scope(interests="Мясо и рыба", glossary=(GlossaryEntry("Отчёт о нанесении", (), "обработка ОтчётНанесение в ТАЛАРИКС MES"),))
    change = make_change(title="Новое поле в отчёте")
    change.analysis = make_analysis()
    change.analysis.summary = "В отчёт о нанесении для мяса добавлено поле «масса нетто»."
    change.analysis.affected_methods = ["POST /doc/create", "поле «масса нетто»"]
    change.analysis.affected_components = ["Отчёт о нанесении"]
    change.analysis.required_actions = ["передавать массу нетто"]
    change.analysis.deadlines = ["01.09.2026 — поле обязательно"]
    change.analysis.nearest_deadline = "01.09.2026"
    subject, text, html_body = render([change], [], scope=scope)

    lines = [" ".join(line.split()) for line in text.splitlines()]  # выравнивание колонок не важно, важен порядок и содержание
    assert "Что: В отчёт о нанесении для мяса добавлено поле «масса нетто»." in lines
    assert "Как: передавать массу нетто" in lines and "Срок: 01.09.2026 — поле обязательно" in lines
    assert "- Отчёт о нанесении → обработка ОтчётНанесение в ТАЛАРИКС MES" in lines and "- POST /doc/create" in lines
    order = [lines.index(next(x for x in lines if x.startswith(prefix))) for prefix in ("Что:", "Где:", "Как:", "Срок:")]
    assert order == sorted(order)
    assert "Кого касается" not in text and "Почему" not in text and "Отбор по теме" not in text  # лишнего нет
    assert "<td style='padding:2px 0;font-weight:bold;color:#c62828'>01.09.2026 — поле обязательно</td>" in html_body
    assert subject.startswith("[Мониторинг] срок: 01.09.2026, затронуто: отчёт о нанесении")


def test_missing_parts_are_shown_as_not_specified_instead_of_being_hidden():
    change = make_change()
    change.analysis = make_analysis()
    change.analysis.affected_methods, change.analysis.required_actions, change.analysis.deadlines = [], [], []
    _, text, html_body = render([change], [])
    lines = [" ".join(line.split()) for line in text.splitlines()]
    assert "Где: не указано" in lines and "Как: в документе не указано" in lines and "Срок: не указан" in lines
    assert "color:#999" in html_body  # серым: отсутствие данных не кричит


def test_deadline_falls_back_to_nearest_deadline_when_list_is_empty():
    change = make_change()
    change.analysis = make_analysis()
    change.analysis.deadlines, change.analysis.nearest_deadline = [], "15.11.2026"
    assert "Срок: 15.11.2026" in [" ".join(line.split()) for line in render([change], [])[1].splitlines()]


def test_changes_are_sorted_by_importance_then_by_nearest_deadline():
    def item(title, importance, deadline):
        c = make_change(title=title)
        c.analysis = make_analysis(importance)
        c.analysis.nearest_deadline = deadline
        return c

    late, soon, none, low = item("поздно", "высокая", "01.12.2026"), item("скоро", "высокая", "01.09.2026"), item("без срока", "высокая", ""), item("мелочь", "низкая", "01.01.2026")
    ordered = sorted([low, none, late, soon], key=sort_key)
    assert [c.record.title for c in ordered] == ["скоро", "поздно", "без срока", "мелочь"]
    assert deadline_date("до 01.09.2026 включительно") == date(2026, 9, 1) and deadline_date("31.02.2026") is None and deadline_date("") is None


def test_pdf_origin_is_shown_in_the_letter():
    change = make_change(title="Памятка.pdf")
    change.analysis = make_analysis()
    change.found_on = "https://example.com/page/"
    _, text, html_body = render([change], [])
    assert "файл со страницы https://example.com/page/" in text and "файл со страницы https://example.com/page/" in html_body


# --- сигналы в письме ---


def test_signals_only_letter_has_alarm_subject_and_section():
    subject, text, html_body = render([], [], signals=[Signal("log", "l", "Журнал: Устаревшее API", "средняя", "12 записей"), high()])
    assert subject == "[Мониторинг] СИГНАЛЫ API: 2 — cises/info: HTTP 410 Gone: метод отключён"  # высокая первой
    assert "Сигналы API и журнала: 2" in text and text.index("[ВЫСОКАЯ]") < text.index("[СРЕДНЯЯ]")
    assert "<b>cises/info</b>: HTTP 410 Gone" in html_body


def test_signals_come_before_documents_and_are_counted_in_the_subject():
    change = make_change()
    change.analysis = make_analysis()
    subject, text, _ = render([change], [], signals=[high()])
    assert subject.startswith("[Мониторинг] СИГНАЛЫ API: 1, обновлений: 1")
    assert text.index("Сигналы API") < text.index("Найдено обновлений")


# --- run с мониторами ---


def pages():
    return {INDEX_URL: index_html([20]), item_url(20): item_html(20)}


class Monitor:
    """Подмена проверки: возвращает заданные сигналы и запоминает, был ли commit."""

    def __init__(self, signals=(), crash=None):
        self.signals, self.crash, self.committed = list(signals), crash, 0

    def __call__(self):
        if self.crash:
            raise self.crash
        return MonitorResult(list(self.signals), lambda: setattr(self, "committed", self.committed + 1), ["проверено"])


def do_run(monitor, *, mailbox=None, dry_run=False, fetcher=None, source=None):
    mailbox = mailbox or Mailbox()
    report = run(
        [source or make_source(initial_notify=1)], fetcher=fetcher or FakeFetcher(pages()), store=make_store(),
        analyzer=FakeAnalyzer(), mailer=mailbox, settings=make_settings(), monitors=[("Контрольные вызовы API", monitor)], dry_run=dry_run,
    )
    return report, mailbox


def test_signals_alone_trigger_a_letter_even_without_document_changes():
    monitor = Monitor([high()])
    report, mailbox = do_run(monitor, fetcher=FakeFetcher({INDEX_URL: index_html([20]), item_url(20): item_html(20)}), source=make_source(initial_notify=0))
    assert report.changes == [] and report.sent and len(mailbox.sent) == 1
    assert mailbox.sent[0][0].startswith("[Мониторинг] СИГНАЛЫ API: 1") and monitor.committed == 1


def test_quiet_monitor_sends_nothing_but_still_saves_its_state():
    monitor = Monitor([])
    report, mailbox = do_run(monitor, source=make_source(initial_notify=0))
    assert not report.sent and mailbox.sent == [] and monitor.committed == 1


def test_monitor_state_is_saved_only_after_the_letter_is_sent():
    monitor = Monitor([high()])
    with pytest.raises(ConnectionError):
        do_run(monitor, mailbox=Mailbox(fail=True))
    assert monitor.committed == 0  # письмо не ушло — завтра сигнал найдётся снова


def test_dry_run_prints_signals_and_saves_nothing(capsys):
    monitor = Monitor([high()])
    report, mailbox = do_run(monitor, dry_run=True)
    assert mailbox.sent == [] and monitor.committed == 0 and report.signals == [high()]
    assert "Сигналы API и журнала: 1" in capsys.readouterr().out


def test_crashing_monitor_becomes_an_alert_and_documents_still_arrive():
    report, mailbox = do_run(Monitor(crash=RuntimeError("упал парсер")))
    assert len(report.changes) == 1 and report.signals == []
    assert any("Контрольные вызовы API: проверка не удалась (RuntimeError: упал парсер)" in a for a in report.alerts)
    assert "Сбои мониторинга" in mailbox.sent[0][1]


# --- CLI ---


def write_config(tmp_path, probes=None, logs=None):
    config = tmp_path / "sources.yaml"
    config.write_text(f"sources:\n  - {{id: cz-releases, type: list, url: '{INDEX_URL}', link_pattern: '{PATTERN}', initial_notify: 0}}\n", encoding="utf-8")
    if probes:
        (tmp_path / "probes.yaml").write_text(probes, encoding="utf-8")
    if logs:
        (tmp_path / "logwatch.yaml").write_text(logs, encoding="utf-8")
    return config


def cli_run(tmp_path, *args):
    return cli.main(["--env", str(tmp_path / "нет.env"), "--log-file", "", *args])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SITEWATCH_DB", str(tmp_path / "state.db"))
    for key, value in {"SMTP_HOST": "smtp.example.com", "MAIL_FROM": "a@x.ru", "MAIL_TO": "b@x.ru"}.items():
        monkeypatch.setenv(key, value)
    return tmp_path


def probe_yaml(api, path="/cises/info"):
    return f"probes:\n  - {{id: cises-info, name: cises/info, url: '{api.base}{path}'}}\n"


def test_probes_command_shows_results_and_saves_only_with_save_flag(env, api, capsys):
    config = write_config(env, probes=probe_yaml(api))
    api.reply("/cises/info", 200, {"results": []})

    assert cli_run(env, "probes", "--config", str(config)) == 0
    out = capsys.readouterr().out
    assert "cises-info: HTTP 200" in out and "Ничего не сохранено" in out
    store = Store(str(env / "state.db"))
    assert store.probe_get("cises-info", "full") is None  # без --save база пуста
    store.close()

    assert cli_run(env, "probes", "--config", str(config), "--save") == 0
    api.reply("/cises/info", 410, {"error_message": "отключено"})
    assert cli_run(env, "probes", "--config", str(config)) == 0
    assert "HTTP 410 Gone: метод отключён" in capsys.readouterr().out


def test_run_dry_run_picks_up_probes_next_to_the_config_and_no_monitors_skips_them(env, api, monkeypatch, capsys):
    config = write_config(env, probes=probe_yaml(api))
    api.reply("/cises/info", 410, {"error_message": "отключено"})
    monkeypatch.setattr(cli, "Fetcher", lambda *a, **k: FakeFetcher({INDEX_URL: index_html([20]), item_url(20): item_html(20)}))

    assert cli_run(env, "run", "--config", str(config), "--dry-run", "--no-llm") == 0
    assert "СИГНАЛЫ API: 1" in capsys.readouterr().out
    assert len(api.requests) == 1

    assert cli_run(env, "run", "--config", str(config), "--dry-run", "--no-llm", "--no-monitors") == 0
    capsys.readouterr()
    assert len(api.requests) == 1  # второй раз к API не ходили


def test_real_run_sends_signal_letter_and_remembers_baseline(env, api, monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "send_email", lambda settings, subject, text, html: sent.append(subject))
    monkeypatch.setattr(cli, "Fetcher", lambda *a, **k: FakeFetcher({INDEX_URL: index_html([20]), item_url(20): item_html(20)}))
    config = write_config(env, probes=probe_yaml(api))
    api.reply("/cises/info", 200, {"a": 1})

    assert cli_run(env, "run", "--config", str(config), "--no-llm") == 0 and sent == []  # первый запуск: тихо
    api.reply("/cises/info", 404, {"error_message": "нет метода"})
    assert cli_run(env, "run", "--config", str(config), "--no-llm") == 0
    assert len(sent) == 1 and sent[0].startswith("[Мониторинг] СИГНАЛЫ API: 1")
    assert cli_run(env, "run", "--config", str(config), "--no-llm") == 0 and len(sent) == 1  # то же самое второй раз не шлём


def test_logwatch_command_and_run_use_the_log_config(env, monkeypatch, capsys):
    log_dir = env / "logs"
    log_dir.mkdir()
    (log_dir / "chz.log").write_text("начало\n", encoding="utf-8")
    config = write_config(env, logs=f"logs:\n  - id: integration\n    paths: ['{log_dir.as_posix()}/*.log']\n    signatures: [{{id: dep, name: Устаревшее API, pattern: 'Устаревшее API'}}]\n")
    assert cli_run(env, "logwatch", "--config", str(config), "--save") == 0
    (log_dir / "chz.log").write_text("начало\nУстаревшее API\n", encoding="utf-8")
    capsys.readouterr()

    monkeypatch.setattr(cli, "Fetcher", lambda *a, **k: FakeFetcher({INDEX_URL: index_html([20]), item_url(20): item_html(20)}))
    assert cli_run(env, "run", "--config", str(config), "--dry-run", "--no-llm") == 0
    out = capsys.readouterr().out
    assert "СИГНАЛЫ API: 1" in out and "Устаревшее API" in out


def test_monitor_commands_explain_missing_and_broken_config(env, capsys, monkeypatch):
    config = write_config(env)
    assert cli_run(env, "probes", "--config", str(config)) == 2 and "probes.yaml" in capsys.readouterr().out
    assert cli_run(env, "logwatch", "--config", str(config)) == 2

    write_config(env, probes="probes:\n  - {id: a}\n")
    assert cli_run(env, "probes", "--config", str(config)) == 2 and "id и url" in capsys.readouterr().out


def test_broken_optional_file_is_a_warning_in_the_letter_and_does_not_stop_the_run(env, monkeypatch, capsys):
    log_dir = env / "logs"
    log_dir.mkdir()
    (log_dir / "chz.log").write_text("начало\n", encoding="utf-8")
    good_logs = f"logs:\n  - id: integration\n    paths: ['{log_dir.as_posix()}/*.log']\n    signatures: [{{id: dep, name: Устаревшее API, pattern: 'Устаревшее API'}}]\n"
    config = write_config(env, probes="probes:\n  - {id: a}\n", logs=good_logs)  # probes.yaml битый, logwatch.yaml в порядке
    monkeypatch.setattr(cli, "Fetcher", lambda *a, **k: FakeFetcher({INDEX_URL: index_html([20]), item_url(20): item_html(20)}))
    monkeypatch.setattr(cli, "send_email", lambda *a, **k: None)
    assert cli_run(env, "run", "--config", str(config), "--no-llm") == 0  # логи прочитаны как baseline, письмо о сбое проб не мешает
    assert cli_run(env, "run", "--config", str(config), "--dry-run", "--no-llm") == 0
    out = capsys.readouterr().out
    assert "Контрольные вызовы API: проверка не удалась (ConfigError" in out and "id и url" in out
    assert "Журнал интеграции: проверка не удалась" not in out  # вторая проверка работает


def test_empty_probe_file_is_just_ignored_in_run(env, monkeypatch, capsys):
    config = write_config(env, probes="probes: []\n")
    monkeypatch.setattr(cli, "Fetcher", lambda *a, **k: FakeFetcher({INDEX_URL: index_html([20]), item_url(20): item_html(20)}))
    assert cli_run(env, "run", "--config", str(config), "--dry-run", "--no-llm") == 0


# --- reset, doctor, меню ---


def test_reset_forgets_probe_and_log_state_too_but_source_reset_leaves_it(env, api, capsys):
    store = Store(str(env / "state.db"))
    store.probe_save("cises-info", "full", '{"status": 200}')
    store.log_save("integration", {"p": (10, "h")}, {})
    store.save_records([])
    store.mark_success("a")
    store.close()

    assert cli_run(env, "reset", "--yes", "--source", "a") == 0
    store = Store(str(env / "state.db"))
    assert store.monitor_state_count() == 2  # сброс одного источника проб и журналов не трогает
    store.close()

    assert cli_run(env, "reset", "--yes") == 0
    out = capsys.readouterr().out
    assert "контрольные вызовы API и журналы интеграции: записей 2" in out
    store = Store(str(env / "state.db"))
    assert store.monitor_state_count() == 0 and store.probe_get("cises-info", "full") is None and not store.log_is_baselined("integration")
    store.close()


def test_reset_with_only_monitor_state_is_not_reported_as_empty_memory(env, capsys):
    store = Store(str(env / "state.db"))
    store.probe_save("p", "full", "{}")
    store.close()
    assert cli_run(env, "reset", "--yes") == 0
    assert "Память очищена" in capsys.readouterr().out


def test_doctor_reports_missing_variables_and_missing_logs(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("TRUE_API_BASE", raising=False)
    probes = tmp_path / "probes.yaml"
    probes.write_text("probes:\n  - {id: auth, url: '${TRUE_API_BASE}/auth/key'}\n  - {id: ok, url: 'https://api.example.com/x', headers: {Authorization: 'Bearer ${TRUE_API_TOKEN}'}}\n", encoding="utf-8")
    logs = tmp_path / "logwatch.yaml"
    logs.write_text(f"logs:\n  - {{id: integration, paths: ['{tmp_path.as_posix()}/нет/*.log'], signatures: [{{id: s, name: Устаревшее API, pattern: x}}]}}\n", encoding="utf-8")
    monkeypatch.delenv("TRUE_API_TOKEN", raising=False)

    assert _doctor_monitors(probes, logs) == 0
    out = capsys.readouterr().out
    assert "auth: не задано: TRUE_API_BASE" in out and "ok: GET api.example.com (без токена)" in out
    assert "проб в файле 2, готово к запуску 1" in out and "TRUE_API_TOKEN не задан" in out
    assert "integration: по путям" in out and "ищем: Устаревшее API" in out


def test_doctor_tolerates_absent_files_and_flags_broken_ones(tmp_path, capsys):
    assert _doctor_monitors(tmp_path / "a.yaml", tmp_path / "b.yaml") == 0
    assert "контрольных вызовов API не будет" in capsys.readouterr().out
    bad = tmp_path / "p.yaml"
    bad.write_text("probes:\n  - {id: x}\n", encoding="utf-8")
    assert _doctor_monitors(bad, tmp_path / "b.yaml") == 1


def test_menu_lists_new_items_in_numeric_order_and_runs_them(capsys):
    calls = []
    run_menu(lambda argv: calls.append(argv) or 0, [], "c.yaml", ".env", "", scripted("12", "", "13", "", "0"))
    out = capsys.readouterr().out
    positions = [out.index(f"  {n}  ") for n in ("1", "7", "8", "11", "12", "13")]
    assert positions == sorted(positions) and out.rindex("  0  Выход") > positions[-1]
    assert calls == [["probes", "--config", "c.yaml"], ["logwatch", "--config", "c.yaml"]]
    assert {key for key, *_ in ACTIONS} >= {"12", "13"}


def test_where_does_not_repeat_a_component_the_agent_also_listed_as_method():
    scope = Scope(interests="Мясо", glossary=(GlossaryEntry("Отчёт о нанесении", (), "обработка ОтчётНанесение"),))
    change = make_change()
    change.analysis = make_analysis()
    change.analysis.affected_components = ["Отчёт о нанесении"]
    change.analysis.affected_methods = ["отчёт о нанесении", "поле «масса нетто»", "Поле «масса нетто»"]
    _, text, _ = render([change], [], scope=scope)
    assert text.count("тчёт о нанесении") == 1 and text.count("масса нетто") == 1
