"""Реестр сроков: даты вступления изменений в силу, которые агент нашёл в документах или которые внесены вручную.

Зачем: ЧЗ часто пишет сроки одной строкой внутри длинного документа, и через месяц о них никто не помнит.
Реестр собирает их в один календарь и присылает напоминания (за 30, 14, 7, 3, 1 день и в день срока),
даже если новых документов в этот день нет.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import replace
from datetime import date
from typing import Iterable

from .models import Change, Deadline

THRESHOLDS = (30, 14, 7, 3, 1, 0)  # за сколько дней напоминать
HORIZON_DAYS = 45  # в письме показываем сроки на столько дней вперёд
OVERDUE_DAYS = 14  # и столько дней после срока: проверить, что сделано

_DATE_RX = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)")
_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}  # fmt: skip
_MONTH_RX = re.compile(
    r"(?<![а-яё])(январ|феврал|март|апрел|ма[йяе](?![а-яё])|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-яё]*\s+(\d{4})",
    re.IGNORECASE,
)
_LEAD_RX = re.compile(r"^\s*(?:до|с|по|не\s+позднее|начиная\s+с)?\s*\d{1,2}\.\d{1,2}\.\d{4}\s*(?:г\.?)?\s*[—–:\-]*\s*", re.IGNORECASE)


def parse_deadline(text: str) -> tuple[date, bool, str] | None:
    """Из строки вида «01.07.2026 — новый срок» достаёт (дата, примерная ли, пояснение).

    Точная дата ДД.ММ.ГГГГ главнее. Если есть только «ноябре 2026» — срок берётся как конец месяца и помечается
    примерным. Строки без даты (например, «с выходом новой версии») в реестр не попадают.
    """
    text = " ".join(str(text).split())
    m = _DATE_RX.search(text)
    if m:
        try:
            due = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
        return due, False, _LEAD_RX.sub("", text, count=1).strip() or text
    m = _MONTH_RX.search(text)
    if m:
        stem = m.group(1).lower()
        month = next(num for key, num in _MONTHS.items() if stem.startswith(key))
        year = int(m.group(2))
        return date(year, month, calendar.monthrange(year, month)[1]), True, text
    return None


def collect_deadlines(change: Change) -> list[Deadline]:
    """Сроки из разбора одного документа (по строкам deadlines и полю nearest_deadline)."""
    a = change.analysis
    if a is None:
        return []
    origin = f"{change.source.name}: {change.record.title}"[:200]
    found: dict[str, Deadline] = {}
    components = tuple(a.affected_components)
    for line in a.deadlines:
        parsed = parse_deadline(line)
        if parsed:
            due, approx, title = parsed
            d = Deadline(due, title[:300], origin, change.record.url, approx, components)
            found.setdefault(d.key, d)
    # nearest_deadline обычно повторяет одну из строк, но без пояснения: берём его, только если такой даты там не было
    parsed = parse_deadline(a.nearest_deadline) if a.nearest_deadline else None
    if parsed and all(d.due != parsed[0] for d in found.values()):
        due, approx, title = parsed
        d = Deadline(due, title[:300], origin, change.record.url, approx, components)
        found.setdefault(d.key, d)
    return list(found.values())


def merge(known: Iterable[Deadline], incoming: Iterable[Deadline]) -> list[Deadline]:
    """Объединяет сохранённые сроки с новыми; состояние напоминаний сохранённых не теряется."""
    by_key = {d.key: d for d in known}
    for d in incoming:
        old = by_key.get(d.key)
        by_key[d.key] = replace(d, reminded_at=old.reminded_at, dismissed=old.dismissed) if old else d
    return sorted(by_key.values(), key=lambda d: (d.due, d.title))


def days_left(d: Deadline, today: date) -> int:
    return (d.due - today).days


def reminder_threshold(d: Deadline, today: date) -> int | None:
    """Порог, по которому пора напомнить сейчас. None — напоминать не нужно (рано, поздно или уже напоминали)."""
    left = days_left(d, today)
    if d.dismissed or left < 0:
        return None
    threshold = min(t for t in THRESHOLDS if t >= left) if left <= THRESHOLDS[0] else None
    if threshold is None:
        return None
    return threshold if d.reminded_at is None or threshold < d.reminded_at else None


def due_reminders(entries: Iterable[Deadline], today: date) -> list[tuple[Deadline, int]]:
    return [(d, t) for d in entries if (t := reminder_threshold(d, today)) is not None]


def upcoming(entries: Iterable[Deadline], today: date) -> list[Deadline]:
    """Сроки для блока в письме: ближайшие и недавно прошедшие, без снятых вручную."""
    return [d for d in entries if not d.dismissed and -OVERDUE_DAYS <= days_left(d, today) <= HORIZON_DAYS]


def describe_days(d: Deadline, today: date) -> str:
    left = days_left(d, today)
    if left == 0:
        return "сегодня"
    if left > 0:
        return f"через {left} дн."
    return f"прошёл {-left} дн. назад"
