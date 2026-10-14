"""Общие модели данных."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Analysis(BaseModel):
    """Краткий разбор изменения для письма: что изменилось, где менять, как менять, к какому сроку.

    Валидаторы прощают типичные огрехи языковой модели (null вместо пустого списка,
    «Высокая» с заглавной буквы), чтобы не терять разбор из-за мелочей формата.
    """

    summary: str = Field(description="Что изменилось: 1-2 коротких предложения только по теме читателя.")
    affected_methods: list[str] = Field(
        default_factory=list,
        description="Где менять: методы API, форматы, отчёты и другие объекты из текста, которых касается изменение.",
    )
    required_actions: list[str] = Field(
        default_factory=list,
        description="Как менять: короткие шаги, что сделать. Пусто, если в тексте не указано.",
    )
    deadlines: list[str] = Field(
        default_factory=list,
        description="Сроки с пояснением, к чему они относятся («01.09.2026 — переход на новый хост»). Пусто, если сроков нет.",
    )
    nearest_deadline: str = Field(default="", description="Ближайший срок, который нужно учесть, как ДД.ММ.ГГГГ. Пусто, если сроков нет.")
    importance: Literal["высокая", "средняя", "низкая"] = Field(description="Важность изменения для читателя.")
    relevance: Literal["прямо", "общее", "не относится"] = Field(
        default="прямо",  # если модель забыла поле, документ не прячем: отсев по теме только по её явному решению
        description="«прямо» — есть изменения по теме читателя; «общее» — касается всех товарных групп или системы "
        "в целом; «не относится» — только про другие товарные группы.",
    )
    relevance_note: str = Field(default="", description="Одна фраза: что отобрано из документа или почему он не относится к теме.")
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

    @field_validator("affected_methods", "deadlines", "required_actions", "affected_components", mode="before")
    @classmethod
    def _as_list(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value

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
    follow_pdf: bool = False  # PDF по ссылкам со страниц источника читать как отдельные документы
    pdf_pattern: str | None = None  # регулярное выражение: какие PDF-ссылки брать (по умолчанию все)
    max_pdf_links: int = 5  # сколько PDF-ссылок с одной страницы обрабатывать


@dataclass(frozen=True)
class GlossaryEntry:
    """Строка таблицы «метод API или термин -> где менять в вашей системе»."""

    name: str
    terms: tuple[str, ...] = ()
    where: str = ""


@dataclass(frozen=True)
class Scope:
    """Что нужно читателю. Пустые interests — фильтра нет, присылается всё найденное."""

    interests: str = ""
    include_general: bool = False  # True — брать и общие изменения (API, авторизация, форматы), не только про вашу тему
    glossary: tuple[GlossaryEntry, ...] = ()


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
    found_on: str = ""  # для PDF по ссылке: адрес страницы, на которой файл найден
    truncated: bool = False  # текст документа не поместился в запрос к агенту целиком
    analysis: Analysis | None = None
