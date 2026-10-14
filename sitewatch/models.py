"""Общие модели данных."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
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
    relevance: Literal["прямо", "общее", "не относится"] = Field(
        default="общее",
        description="«прямо» — есть изменения по теме читателя; «общее» — касается всех товарных групп или системы "
        "в целом; «не относится» — только про другие товарные группы.",
    )
    relevance_note: str = Field(default="", description="Одна фраза: что отобрано из документа или почему он не относится к теме.")
    nearest_deadline: str = Field(default="", description="Ближайший срок, который нужно учесть, как ДД.ММ.ГГГГ. Пусто, если сроков нет.")
    affected_components: list[str] = Field(
        default_factory=list, description="Названия записей из таблицы соответствий, которых касается изменение."
    )

    @field_validator("relevance", mode="before")
    @classmethod
    def _relevance_norm(cls, value):
        if not isinstance(value, str):
            return value
        v = value.strip().lower()
        if v.startswith("не относ"):
            return "не относится"
        return "прямо" if v.startswith("прям") else v

    @field_validator("nearest_deadline", mode="before")
    @classmethod
    def _deadline_text(cls, value):
        return "" if value is None else str(value).strip()

    @field_validator("key_changes", "deadlines", "required_actions", "affected_components", mode="before")
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
    type: str  # page | list | sitemap | pdf | text | telegram
    url: str
    focus: str = ""
    link_pattern: str | None = None
    content_selector: str | None = None
    ignore_regex: tuple[str, ...] = ()
    recheck_latest: int = 0
    initial_notify: int = 0
    max_new_per_run: int = 10
    enabled: bool = True  # выключенный источник не участвует в run, но его можно проверить через check-sources
    robots: str = ""  # respect | warn | off; пусто — как в настройках (SITEWATCH_ROBOTS)
    strip_query: bool = False  # у найденных ссылок отбрасывается ?query: одна тема форума с разными параметрами = один документ
    fetch: str = "http"  # http | browser: browser — страницу открывает настоящий браузер (для сайтов с JS и антиботом)


@dataclass(frozen=True)
class GlossaryEntry:
    """Строка таблицы «метод API или термин -> где менять в вашей системе»."""

    name: str
    terms: tuple[str, ...] = ()
    where: str = ""


@dataclass(frozen=True)
class Deadline:
    """Срок из реестра: найден агентом в документе или внесён вручную (config/milestones.yaml)."""

    due: date
    title: str
    origin: str = ""  # откуда срок: «название источника: заголовок документа» или «вручную»
    url: str = ""
    approx: bool = False  # в документе указан только месяц: срок взят как конец месяца
    components: tuple[str, ...] = ()  # записи глоссария, которых касается срок
    note: str = ""
    manual: bool = False
    reminded_at: int | None = None  # самый малый порог напоминания (в днях), о котором уже писали
    dismissed: bool = False  # снят вручную (sitewatch deadlines --done)

    @property
    def key(self) -> str:
        """Короткий устойчивый идентификатор: одинаковый срок из разных запусков не дублируется."""
        norm = re.sub(r"\W+", " ", self.title.casefold()).strip()
        raw = f"{self.due.isoformat()}|{norm}|{'manual' if self.manual else self.url}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class Scope:
    """Что нужно читателю. Пустые interests — фильтра нет, присылается всё найденное."""

    interests: str = ""
    include_general: bool = True  # общие изменения (API, авторизация, форматы) касаются и вашей темы
    glossary: tuple[GlossaryEntry, ...] = ()
    milestones: tuple[Deadline, ...] = ()  # известные вручную сроки из config/milestones.yaml


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
