"""Привязка «мест в документе» от агента к тексту: проверка, что цитата настоящая, и ссылка, которая ведёт к месту.

Агент называет раздел, дословную цитату и что менять. Языковая модель может ошибиться или придумать, поэтому:
  - цитата принимается, только если дословно (с точностью до регистра, пробелов, кавычек и тире) есть в тексте документа;
  - ссылка строится не из слов модели, а из найденной строки документа: для страницы это «текстовый фрагмент»
    (#:~:text=...: браузер откроет страницу сразу на этом месте и подсветит его), для PDF — номер страницы (#page=N).
Место, цитату которого в тексте не нашли, из письма убирается целиком: если цитата выдумана, то раздел и «что менять»
при ней вряд ли надёжнее. Показывается только то, что удалось подтвердить.
"""

from __future__ import annotations

import bisect
import logging
import re
from urllib.parse import quote as urlquote

from .extract import is_file_url
from .models import Change

log = logging.getLogger(__name__)

MIN_QUOTE_CHARS = 8  # короче — не цитата, а случайный обрывок, который найдётся где угодно
MAX_PLACES = 3
WORDS_WHOLE = 10  # строка не длиннее — подсвечивается целиком, иначе от начала до конца
WORDS_START, WORDS_END = 5, 4

_FILE_LINK = re.compile(r"\[https?://[^\]\s]*\]")  # приписки « [адрес]» после ссылок на файлы: в самой странице их нет
_QUOTES = re.compile(r"[«»“”„\"'`|]")
_DASHES = re.compile(r"[‐‑‒–—−]")
_ELLIPSIS = re.compile(r"…|\.{3}")
_LIST_MARK = re.compile(r"^\s*(?:[-+•]\s+)")


def _norm(text: str) -> str:
    text = text.casefold().replace("ё", "е")
    text = _FILE_LINK.sub(" ", text)
    text = _QUOTES.sub(" ", text)
    text = _DASHES.sub("-", text)
    return " ".join(text.split())


def _norm_line(line: str) -> str:
    return _norm(_LIST_MARK.sub("", line))


class _Document:
    """Текст документа в сравнимом виде: одна строка и позиции, с которых начинается каждая настоящая строка."""

    def __init__(self, text: str):
        self.lines = text.splitlines()
        parts, self.starts, self.line_index, pos = [], [], [], 0
        for index, line in enumerate(self.lines):
            norm = _norm_line(line)
            if not norm:
                continue
            parts.append(norm)
            self.starts.append(pos)
            self.line_index.append(index)
            pos += len(norm) + 1
        self.norm = " ".join(parts)

    def find(self, quote: str) -> int | None:
        """Индекс строки документа, с которой начинается цитата; None, если цитаты в тексте нет."""
        pieces = []
        for part in _ELLIPSIS.split(quote):  # модель часто сокращает цитату многоточием: каждый кусок должен быть в тексте по порядку
            norm = " ".join(_norm_line(line.lstrip("+")) for line in part.splitlines())
            norm = " ".join(norm.split())
            if len(norm) >= MIN_QUOTE_CHARS:
                pieces.append(norm)
        if not pieces:
            return None
        position, first = 0, None
        for norm in pieces:
            found = self.norm.find(norm, position)
            if found < 0:
                return None
            first = found if first is None else first
            position = found + len(norm)
        return self.line_index[bisect.bisect_right(self.starts, first) - 1]


def text_fragment(line: str, quote_norm: str = "") -> str:
    """Текстовый фрагмент для ссылки (#:~:text=...) по строке документа. Пусто, если подсвечивать нечего."""
    cells = [c for c in re.split(r"\s+\|\s+", _LIST_MARK.sub("", line)) if c.strip()]
    cell = next((c for c in cells if quote_norm[:12] and quote_norm[:12] in _norm(c)), cells[0] if cells else "")
    words = _FILE_LINK.sub("", cell).split()
    if not words:
        return ""
    if len(words) <= WORDS_WHOLE:
        start, end = " ".join(words), ""
    else:
        start, end = " ".join(words[:WORDS_START]), " ".join(words[-WORDS_END:])

    def encode(text: str) -> str:
        return urlquote(text, safe="").replace("-", "%2D")  # дефис в директиве означает префикс/суффикс

    return ":~:text=" + encode(start) + ("," + encode(end) if end else "")


def ground_places(change: Change) -> None:
    """Проверяет места из разбора по тексту документа и строит на них ссылки. Не бросает исключений: письмо важнее."""
    analysis = change.analysis
    if analysis is None or not analysis.places:
        return
    try:
        _ground(change)
    except Exception:  # noqa: BLE001 — сбой привязки не должен лишать письмо самого разбора
        log.exception("Не удалось привязать места к тексту документа «%s»", change.record.title)
        analysis.places = []  # проверить нечем, а непроверенные места не показываем


def _ground(change: Change) -> None:
    record, analysis = change.record, change.analysis
    analysis.places = analysis.places[:MAX_PLACES]
    document = _Document(record.text)
    is_pdf = record.line_pages is not None
    plain_link = is_file_url(record.url) or change.source.type in ("telegram", "text")  # якоря на таких адресах не работают

    for place in analysis.places:
        place.url, place.page, place.verified = record.url, None, False
        index = document.find(place.quote) if place.quote else None
        if index is None:
            log.info("  место отброшено, цитата не найдена в тексте: «%s»", place.quote[:80])
            continue
        place.verified = True
        if is_pdf:
            page = record.line_pages[index] if index < len(record.line_pages) else 0
            if page:
                place.page, place.url = page, f"{record.url}#page={page}"
        elif not plain_link:
            fragment = text_fragment(document.lines[index], _norm_line(place.quote.splitlines()[0].lstrip("+")))
            if fragment:
                place.url = f"{record.url}#{fragment}"
    analysis.places = [place for place in analysis.places if place.verified]
