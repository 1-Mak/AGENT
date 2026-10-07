"""Интерактивное меню: все действия без запоминания команд."""

from __future__ import annotations

import subprocess
import sys
import traceback
from pathlib import Path
from typing import Callable

# (пункт, подпись, аргументы sitewatch, принимает ли --config)
ACTIONS = [
    ("1", "Проверка настроек и окружения (doctor)", ["doctor"], True),
    ("2", "Проверка источников: что видит парсер (базу не трогает)", ["check-sources"], True),
    ("3", "Проверка DeepSeek: ключ, модель, расход токенов", ["test-llm"], False),
    ("4", "Отправить тестовое письмо", ["test-email"], False),
    ("5", "Пробный запуск: показать письмо, ничего не отправлять и не сохранять", ["run", "--dry-run"], True),
    ("6", "Пробный запуск без DeepSeek", ["run", "--dry-run", "--no-llm"], True),
    ("7", "БОЕВОЙ запуск: отправить письмо и сохранить состояние", ["run"], True),
]
EXTRA = [
    ("8", "Автозапуск раз в сутки: включить / проверить / выключить"),
    ("9", "Открыть .env для редактирования"),
    ("10", "Запустить тесты"),
    ("0", "Выход"),
]
EXIT_WORDS = {"0", "q", "quit", "exit", "в", "выход"}


def _print_menu(log_file: str) -> None:
    print("\n" + "=" * 70)
    print(" sitewatch — мониторинг обновлений документации")
    print("=" * 70)
    for key, label, _, _ in ACTIONS:
        print(f"  {key}  {label}")
    for key, label in EXTRA:
        print(f"  {key}  {label}")
    print("-" * 70)
    print(f" Папка: {Path.cwd()}")
    if log_file:
        print(f" Журнал работы: {log_file}")
    print(" Первый раз: пункты 1 -> 2 -> 3 -> 4 -> 5, и только потом 7.")


def _open_env(env_path: str) -> None:
    path = Path(env_path)
    if not path.exists():
        example = Path(".env.example")
        if example.exists():
            path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"Создан {path} из .env.example")
    if sys.platform.startswith("win"):
        print(f"Открываю {path.resolve()} в Блокноте. Сохраните файл и закройте окно, чтобы вернуться.")
        subprocess.run(["notepad", str(path)], check=False)
    else:
        print(f"Отредактируйте файл: {path.resolve()}")


def _run_tests() -> int:
    if not Path("tests").is_dir():
        print("Папки tests нет — тесты недоступны в этой копии проекта.")
        return 1
    probe = subprocess.run([sys.executable, "-c", "import pytest"], capture_output=True)
    if probe.returncode != 0:
        print("Устанавливаю pytest ...")
        if subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "pytest"]).returncode:
            print("Не удалось установить pytest.")
            return 1
    return subprocess.run([sys.executable, "-m", "pytest", "-q"]).returncode


def _schedule_menu(cli: Callable[[list[str]], int], base_args: list[str], input_fn: Callable[[str], str]) -> int:
    """Показывает состояние автозапуска и предлагает включить или выключить его."""
    print(f"\n>>> sitewatch {' '.join(base_args + ['schedule', 'status'])}\n")
    rc = cli(base_args + ["schedule", "status"])
    print("\n  1  Включить (или изменить время) ежедневный запуск\n  2  Выключить автозапуск\n  Enter  назад")
    answer = input_fn("Выберите: ").strip()
    if answer == "1":
        at = input_fn("Во сколько запускать каждый день по времени этого компьютера? [08:30] ").strip() or "08:30"
        rc = cli(base_args + ["schedule", "install", "--time", at])
    elif answer == "2":
        rc = cli(base_args + ["schedule", "remove"])
    return rc


def run_menu(
    cli: Callable[[list[str]], int],
    base_args: list[str],
    config: str,
    env_path: str,
    log_file: str = "",
    input_fn: Callable[[str], str] = input,
) -> int:
    """Показывает меню, пока пользователь не выберет выход. cli — функция main(argv) -> код возврата."""
    by_key = {key: (cmd, uses_config) for key, _, cmd, uses_config in ACTIONS}
    while True:
        _print_menu(log_file)
        try:
            choice = input_fn("\nВыберите пункт: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in EXIT_WORDS:
            return 0

        rc = 0
        if choice in by_key:
            cmd, uses_config = by_key[choice]
            if choice == "7":
                answer = input_fn("Письмо уйдёт получателям из MAIL_TO, состояние сохранится. Продолжить? [y/N] ")
                if answer.strip().lower() not in ("y", "yes", "д", "да"):
                    print("Отменено.")
                    continue
            argv = base_args + cmd + (["--config", config] if uses_config else [])
            print(f"\n>>> sitewatch {' '.join(argv)}\n")
            try:
                rc = cli(argv)
            except SystemExit as e:  # ошибка аргументов argparse и т.п.
                rc = e.code if isinstance(e.code, int) else 1
            except Exception:  # noqa: BLE001 — меню не должно закрываться из-за ошибки в команде
                traceback.print_exc()
                rc = 1
        elif choice == "8":
            rc = _schedule_menu(cli, base_args, input_fn)
        elif choice == "9":
            _open_env(env_path)
            continue
        elif choice == "10":
            rc = _run_tests()
        else:
            print(f"Нет такого пункта: {choice!r}")
            continue

        print(f"\nГотово, код завершения: {rc} ({'успешно' if rc == 0 else 'есть проблемы — смотрите вывод выше'})")
        try:
            input_fn("Нажмите Enter, чтобы вернуться в меню...")
        except (EOFError, KeyboardInterrupt):
            print()
            return rc
