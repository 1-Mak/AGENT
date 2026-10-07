"""Автозапуск раз в сутки: задание в Планировщике Windows (на других системах — подсказка для cron)."""

from __future__ import annotations

import getpass
import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

from . import ui

log = logging.getLogger(__name__)

TASK_NAME = "sitewatch"
DEFAULT_TIME = "08:30"
Runner = Callable[[list[str]], tuple[int, str]]

# Ключи вывода `schtasks /Query /V /FO LIST` на английской и русской Windows
_STATUS_KEYS = re.compile(
    r"^(Status|Next Run Time|Last Run Time|Last Result|Состояние|Время следующего запуска|"
    r"Время прошлого запуска|Результат прошлого запуска|Последний результат)\s*:",
    re.IGNORECASE,
)


def parse_time(value: str) -> str:
    """'8:30' -> '08:30'. ValueError, если это не время суток."""
    match = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", value or "")
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ValueError(f"время нужно указать как ЧЧ:ММ, например {DEFAULT_TIME} (получено: {value!r})")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def project_run_bat() -> Path:
    return Path(__file__).resolve().parent.parent / "run.bat"


def _task_command(run_bat: Path) -> tuple[str, str]:
    """Что запускает Планировщик: свёрнутое окно, ожидание завершения (чтобы видеть код возврата)."""
    return "cmd.exe", f'/c start "{TASK_NAME}" /min /wait "{run_bat}" auto'


def build_task_xml(run_bat: Path, at: str, user: str, start_date: date) -> str:
    """Определение задания в формате Планировщика. В отличие от schtasks /SC DAILY умеет «наверстать»
    пропущенный запуск, если компьютер в это время был выключен (StartWhenAvailable)."""
    command, arguments = _task_command(run_bat)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Ежедневная проверка обновлений документации (sitewatch)</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start_date.isoformat()}T{at}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(run_bat.parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _run_schtasks(args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(["schtasks", *args], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, f"{type(e).__name__}: {e}"
    raw = proc.stdout + proc.stderr
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp866", errors="replace")  # консоль русской Windows без chcp 65001
    return proc.returncode, text.strip()


def _current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{getpass.getuser()}" if domain else getpass.getuser()


def _linux_hint(at: str) -> None:
    hour, minute = at.split(":")
    ui.warn("Планировщик Windows здесь недоступен. На Linux/macOS добавьте строку в crontab (команда `crontab -e`):")
    ui.info(f"{int(minute)} {int(hour)} * * * cd {project_run_bat().parent} && .venv/bin/sitewatch run >> logs/cron.log 2>&1")


def install(at: str, *, run_bat: Path | None = None, runner: Runner = _run_schtasks, windows: bool | None = None,
            user: str | None = None, today: date | None = None) -> int:
    try:
        at = parse_time(at)
    except ValueError as e:
        ui.fail(str(e))
        return 2
    if not (sys.platform.startswith("win") if windows is None else windows):
        _linux_hint(at)
        return 1
    run_bat = run_bat or project_run_bat()
    if not run_bat.is_file():
        ui.fail(f"не найден {run_bat}. Автозапуск настраивается из папки, где лежит run.bat")
        return 1

    xml_text = build_task_xml(run_bat, at, user or _current_user(), today or date.today())
    fd, xml_path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(xml_path).write_text(xml_text, encoding="utf-16")  # Планировщик ждёт UTF-16 с меткой порядка байт
        rc, output = runner(["/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"])
    finally:
        Path(xml_path).unlink(missing_ok=True)

    catch_up = True
    if rc != 0:
        log.warning("Создание через XML не удалось (%s), пробую упрощённый способ", output)
        command, arguments = _task_command(run_bat)
        rc, output = runner(["/Create", "/TN", TASK_NAME, "/SC", "DAILY", "/ST", at, "/TR", f"{command} {arguments}", "/F"])
        catch_up = False
    if rc != 0:
        ui.fail(f"Планировщик не создал задание: {output}")
        ui.info("Попробуйте запустить программу от имени пользователя, у которого есть права на Планировщик заданий.")
        return 1

    ui.ok(f"Автозапуск включён: каждый день в {at} (время вашего компьютера), задание «{TASK_NAME}»")
    ui.info("Работает, пока вы вошли в Windows и компьютер включён.")
    if catch_up:
        ui.info("Если в это время компьютер был выключен, проверка выполнится при ближайшем включении.")
    else:
        ui.warn("Пропущенные запуски (компьютер выключен) не наверстываются: проверка просто пройдёт на следующий день.")
    ui.info("Журнал каждого запуска: logs\\sitewatch.log. Проверить задание: пункт «Автозапуск» -> статус.")
    return 0


def remove(*, runner: Runner = _run_schtasks, windows: bool | None = None) -> int:
    if not (sys.platform.startswith("win") if windows is None else windows):
        ui.warn("Планировщик Windows здесь недоступен; на Linux/macOS уберите строку из crontab (`crontab -e`).")
        return 1
    rc, output = runner(["/Delete", "/TN", TASK_NAME, "/F"])
    if rc == 0:
        ui.ok(f"Автозапуск выключен, задание «{TASK_NAME}» удалено")
        return 0
    ui.warn(f"Задание «{TASK_NAME}» не найдено — автозапуск и не был включён ({output})")
    return 0


def last_run_from_log(log_file: str) -> str | None:
    """Последняя строка итога из журнала — это и есть «чем закончился прошлый запуск»."""
    try:
        lines = Path(log_file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines[-500:]):
        if "Готово:" in line or "Письмо не отправлено" in line or "завершился неожиданной ошибкой" in line:
            return line
    return None


def status(*, runner: Runner = _run_schtasks, windows: bool | None = None, log_file: str = "logs/sitewatch.log") -> int:
    if not (sys.platform.startswith("win") if windows is None else windows):
        ui.warn("Планировщик Windows здесь недоступен; на Linux/macOS посмотрите `crontab -l`.")
        return 0
    rc, output = runner(["/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"])
    if rc != 0:
        ui.warn("Автозапуск не настроен (задания «sitewatch» в Планировщике нет).")
    else:
        ui.ok(f"Автозапуск настроен, задание «{TASK_NAME}»:")
        shown = [line.strip() for line in output.splitlines() if _STATUS_KEYS.match(line.strip())]
        for line in shown or output.splitlines()[:12]:
            ui.info(line)
    last = last_run_from_log(log_file)
    if last:
        ui.info(f"Итог последнего запуска по журналу: {last}")
    elif rc == 0:
        ui.info("В журнале пока нет записей о запусках.")
    return 0


def run_command(command: str, at: str = DEFAULT_TIME, log_file: str = "logs/sitewatch.log") -> int:
    if command == "install":
        return install(at)
    if command == "remove":
        return remove()
    return status(log_file=log_file)
