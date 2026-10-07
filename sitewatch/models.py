"""Общие модели данных."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Analysis(BaseModel):
    """Структурированный разбор изменения — его заполняет агент-аналитик.

    Валидаторы прощают типичные огрехи языковой модели (null вместо пустого списка,
    «Высокая» с заглавной буквы), чтобы не терять разбор из-за мелочей формата.
    """

    summary: str = Field(description="Суть изменения в 2-4 предложениях.")
    key_changes: list[str] = Field(
        default_factory=list, description="Конкретные изменения по пунктам: что добавили, убрали, поменяли."
    )
    who_is_affected: str = Field(
        default="не указано", description="Кого касается изменение. Если в тексте не сказано — «не указано»."
    )
    deadlines: list[str] = Field(
        default_factory=list,
        description="Сроки и даты вступления в силу с пояснением, к чему они относятся. Пусто, если сроков нет.",
    )
    required_actions: list[str] = Field(
        default_factory=list,
        description="Что нужно сделать читателю согласно тексту документа. Пусто, если не указано.",
    )
    importance: Literal["высокая", "средняя", "низкая"] = Field(description="Важность изменения для читателя.")
    importance_reason: str = Field(default="", description="Одно предложение: почему выбрана такая важность.")

    @field_validator("key_changes", "deadlines", "required_actions", mode="before")
    @classmethod
    def _as_list(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value

    @field_validator("who_is_affected", mode="before")
    @classmethod
    def _affected_default(cls, value):
        return "не указано" if value is None or value == "" else value

    @field_validator("importance", mode="before")
    @classmethod
    def _importance_case(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    type: str  # page | list | sitemap
    url: str
    focus: str = ""
    link_pattern: str | None = None
    content_selector: str | None = None
    ignore_regex: tuple[str, ...] = ()
    recheck_latest: int = 0
    initial_notify: int = 0
    max_new_per_run: int = 10


@dataclass
class DocRecord:
    """Снимок документа, который хранится в базе."""

    url: str
    source_id: str
    title: str
    content_hash: str  # пустая строка = URL известен, но текст ещё не сохранён
    text: str


@dataclass
class Change:
    source: Source
    record: DocRecord
    kind: str  # "new" | "updated"
    diff: str = ""
    truncated: bool = False  # текст документа не поместился в запрос к агенту целиком
    analysis: Analysis | None = None
