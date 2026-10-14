"""Сигналы о состоянии API и журнала интеграции: общий формат для probes и logwatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

LEVEL_ORDER = {"высокая": 0, "средняя": 1, "низкая": 2}


@dataclass(frozen=True)
class Signal:
    """Одна строка в разделе «Сигналы API» письма."""

    source: str  # "probe" | "log"
    key: str  # идентификатор проверки (для логов и тестов)
    title: str  # короткое название: «cises/info», «Журнал: Устаревшее API»
    level: str  # "высокая" | "средняя" | "низкая"
    message: str  # что произошло, одной-двумя строками


def sort_signals(signals: list[Signal]) -> list[Signal]:
    return sorted(signals, key=lambda s: LEVEL_ORDER.get(s.level, 3))


@dataclass
class MonitorResult:
    """Итог одной проверки (пробы API или журнала): сигналы для письма и действие, которое запоминает новое состояние.

    commit вызывается только после успешной отправки письма: если письмо не ушло, при следующем запуске сигналы
    найдутся заново, ничего не теряется."""

    signals: list[Signal] = field(default_factory=list)
    commit: Callable[[], None] = lambda: None
    notes: list[str] = field(default_factory=list)  # что проверено, для журнала
