"""Автозапуск: проверяем всё, что можно проверить без Windows — XML задания, команды schtasks, разбор ответов."""

import subprocess
import xml.etree.ElementTree as ET
from datetime import date
from types import SimpleNamespace

import pytest

from sitewatch import scheduler
from sitewatch.scheduler import build_task_xml, install, last_run_from_log, parse_time, remove, status

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class FakeSchtasks:
    """Запоминает вызовы schtasks и содержимое XML-файла, пока он ещё существует."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.xml = None

    def __call__(self, args):
        self.calls.append(args)
        if "/XML" in args:
            path = args[args.index("/XML") + 1]
            with open(path, "rb") as fh:
                self.xml = fh.read()
        return self.results.pop(0) if self.results else (0, "")


@pytest.fixture
def run_bat(tmp_path):
    folder = tmp_path / "Мой проект"  # пробел и кириллица в пути — обычное дело на Windows
    folder.mkdir()
    bat = folder / "run.bat"
    bat.write_text("@echo off\r\n", encoding="ascii")
    return bat


# --- время ---


@pytest.mark.parametrize("raw, expected", [("8:30", "08:30"), ("08:30", "08:30"), (" 23:59 ", "23:59"), ("9.05", "09:05"), ("0:00", "00:00")])
def test_parse_time_accepts_human_input(raw, expected):
    assert parse_time(raw) == expected


@pytest.mark.parametrize("raw", ["", "25:00", "12:60", "полдень", "8", "08:3"])
def test_parse_time_rejects_nonsense(raw):
    with pytest.raises(ValueError, match="ЧЧ:ММ"):
        parse_time(raw)


# --- XML задания ---


def test_task_xml_is_valid_and_has_the_important_settings(run_bat):
    xml_text = build_task_xml(run_bat, "08:30", "PC\\Иван & Co", date(2026, 10, 8))
    root = ET.fromstring(xml_text.split("?>", 1)[1])  # ElementTree не принимает объявление UTF-16 в строке str

    assert root.find("t:Triggers/t:CalendarTrigger/t:StartBoundary", NS).text == "2026-10-08T08:30:00"
    assert root.find("t:Triggers/t:CalendarTrigger/t:ScheduleByDay/t:DaysInterval", NS).text == "1"
    settings = root.find("t:Settings", NS)
    assert settings.find("t:StartWhenAvailable", NS).text == "true"  # наверстать запуск после выключенного компьютера
    assert settings.find("t:DisallowStartIfOnBatteries", NS).text == "false"  # ноутбук от батареи тоже запускаем
    assert settings.find("t:RunOnlyIfNetworkAvailable", NS).text == "true"
    assert root.find("t:Principals/t:Principal/t:UserId", NS).text == "PC\\Иван & Co"  # экранирование сработало

    exec_node = root.find("t:Actions/t:Exec", NS)
    assert exec_node.find("t:Command", NS).text == "cmd.exe"
    arguments = exec_node.find("t:Arguments", NS).text
    assert f'"{run_bat}" auto' in arguments and "/min" in arguments and "/wait" in arguments  # путь с пробелом в кавычках
    assert exec_node.find("t:WorkingDirectory", NS).text == str(run_bat.parent)


# --- установка ---


def do_install(fake, run_bat, **kw):
    params = dict(run_bat=run_bat, runner=fake, windows=True, user="PC\\user", today=date(2026, 10, 8))
    params.update(kw)
    return install("8:30", **params)


def test_install_creates_task_from_utf16_xml(run_bat, capsys):
    fake = FakeSchtasks((0, "SUCCESS"))
    assert do_install(fake, run_bat) == 0

    args = fake.calls[0]
    assert args[:4] == ["/Create", "/TN", "sitewatch", "/XML"] and args[-1] == "/F"
    assert fake.xml.startswith((b"\xff\xfe", b"\xfe\xff"))  # UTF-16 с меткой порядка байт, как ждёт Планировщик
    decoded = fake.xml.decode("utf-16")
    assert "2026-10-08T08:30:00" in decoded and str(run_bat) in decoded
    out = capsys.readouterr().out
    assert "Автозапуск включён: каждый день в 08:30" in out and "при ближайшем включении" in out


def test_install_falls_back_to_simple_daily_task_when_xml_is_rejected(run_bat, capsys):
    fake = FakeSchtasks((1, "ERROR: incorrectly formatted"), (0, "SUCCESS"))
    assert do_install(fake, run_bat) == 0

    fallback = fake.calls[1]
    assert fallback[:2] == ["/Create", "/TN"] and ["/SC", "DAILY", "/ST", "08:30"] == fallback[fallback.index("/SC"):fallback.index("/SC") + 4]
    command = fallback[fallback.index("/TR") + 1]
    assert command.startswith("cmd.exe /c start") and str(run_bat) in command
    assert "не наверстываются" in capsys.readouterr().out  # честно говорим об ограничении упрощённого способа


def test_install_reports_failure_when_both_ways_fail(run_bat, capsys):
    fake = FakeSchtasks((1, "Access is denied"), (1, "Access is denied"))
    assert do_install(fake, run_bat) == 1
    assert "Access is denied" in capsys.readouterr().out


def test_install_validates_time_platform_and_files(tmp_path, run_bat, capsys):
    fake = FakeSchtasks()
    assert install("25:99", run_bat=run_bat, runner=fake, windows=True) == 2 and fake.calls == []

    assert install("8:05", run_bat=run_bat, runner=fake, windows=False) == 1  # не Windows: подсказка про cron, ничего не создаём
    out = capsys.readouterr().out
    assert "5 8 * * *" in out and "crontab" in out and fake.calls == []

    assert install("8:30", run_bat=tmp_path / "нет" / "run.bat", runner=fake, windows=True) == 1
    assert "не найден" in capsys.readouterr().out


# --- удаление и статус ---


def test_remove_reports_both_outcomes_without_failing(capsys):
    assert remove(runner=FakeSchtasks((0, "SUCCESS")), windows=True) == 0
    assert "Автозапуск выключен" in capsys.readouterr().out
    assert remove(runner=FakeSchtasks((1, "ERROR: not found")), windows=True) == 0
    assert "и не был включён" in capsys.readouterr().out


RU_OUTPUT = """Имя компьютера:                       PC
Имя задачи:                           \\sitewatch
Время следующего запуска:             09.10.2026 8:30:00
Состояние:                            Готово
Режим входа в систему:                Только интерактивный
Время прошлого запуска:               08.10.2026 8:30:01
Результат прошлого запуска:           0
Автор:                                PC\\user"""


def test_status_shows_only_key_fields_of_russian_windows_output(tmp_path, capsys):
    log = tmp_path / "sitewatch.log"
    log.write_text("09:50:16 INFO Старт\n09:50:17 INFO Готово: источников 1 (сбоев 0), обновлений 2\n", encoding="utf-8")
    assert status(runner=FakeSchtasks((0, RU_OUTPUT)), windows=True, log_file=str(log)) == 0
    out = capsys.readouterr().out
    assert "Время следующего запуска" in out and "Результат прошлого запуска" in out
    assert "Режим входа" not in out and "Автор" not in out  # лишнее не показываем
    assert "обновлений 2" in out  # итог прошлого запуска берётся из журнала


def test_status_when_not_configured(tmp_path, capsys):
    assert status(runner=FakeSchtasks((1, "ERROR: not found")), windows=True, log_file=str(tmp_path / "нет.log")) == 0
    assert "не настроен" in capsys.readouterr().out


def test_last_run_from_log_picks_the_latest_outcome(tmp_path):
    log = tmp_path / "x.log"
    log.write_text("1 Готово: старый\n2 INFO шаг\n3 ERROR Письмо не отправлено: сервер недоступен\n4 INFO шаг\n", encoding="utf-8")
    assert "Письмо не отправлено" in last_run_from_log(str(log))
    assert last_run_from_log(str(tmp_path / "нет.log")) is None


def test_schtasks_output_is_decoded_from_utf8_and_from_cp866(monkeypatch):
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(stdout="Состояние: Готово".encode("cp866"), stderr=b"", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert scheduler._run_schtasks(["/Query"]) == (0, "Состояние: Готово")

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: SimpleNamespace(stdout="Готово".encode("utf-8"), stderr=b"", returncode=0))
    assert scheduler._run_schtasks(["/Query"]) == (0, "Готово")

    def missing(cmd, **kwargs):
        raise FileNotFoundError("schtasks")

    monkeypatch.setattr(subprocess, "run", missing)
    rc, text = scheduler._run_schtasks(["/Query"])
    assert rc == 1 and "FileNotFoundError" in text
