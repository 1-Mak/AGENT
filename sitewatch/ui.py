"""Оформление консольного вывода: метки состояния и цвета (только если консоль их поддерживает)."""

from __future__ import annotations

import os
import sys

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
if _COLOR and os.name == "nt":
    os.system("")  # включает обработку ANSI-цветов в консоли Windows 10+


def _paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def title(text: str) -> None:
    print(f"\n{_paint('1;36', text)}\n{'-' * len(text)}")


def ok(text: str) -> None:
    print(f" {_paint('32', '[ OK ]')} {text}")


def warn(text: str) -> None:
    print(f" {_paint('33', '[ !  ]')} {text}")


def fail(text: str) -> None:
    print(f" {_paint('31', '[FAIL]')} {text}")


def info(text: str) -> None:
    print(f"        {text}")


def mask(secret: str) -> str:
    """Показывает, что значение задано, но не раскрывает его."""
    if not secret:
        return "не задан"
    if len(secret) <= 8:
        return f"задан ({len(secret)} симв.)"
    return f"{secret[:3]}…{secret[-2:]} ({len(secret)} симв.)"
