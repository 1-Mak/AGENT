"""Счётчики ошибок в журнале интеграции: рост «Устаревшее API» или «Токен не действителен» — признак аварийной смены API.

Как работает:
  - при первом запуске журнал не читается, запоминается только его размер (старую историю не разбираем);
  - дальше каждый запуск читает только дописанное с прошлого раза и считает строки, подходящие под шаблоны;
  - сигнал, если совпадений не меньше min_count и не меньше growth × среднее за прошлые проверки;
  - в письмо попадают только числа и имена файлов, строки журнала (в них бывают токены и персональные данные) — нет.
"""

from __future__ import annotations

import glob
import hashlib
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import ConfigError
from .signals import MonitorResult, Signal
from .store import Store

log = logging.getLogger(__name__)

HEAD_BYTES = 256
DEFAULT_MAX_READ_MB = 200.0
HISTORY_RUNS = 7


@dataclass(frozen=True)
class LogSignature:
    id: str
    name: str
    regex: re.Pattern
    min_count: int = 1
    growth: float = 3.0


@dataclass(frozen=True)
class LogSpec:
    id: str
    name: str
    paths: tuple[str, ...]
    signatures: tuple[LogSignature, ...]
    encoding: str = "auto"  # auto: UTF-8, а если не получилось — cp1251 (типично для Windows)
    max_read_mb: float = DEFAULT_MAX_READ_MB


def load_logwatch(path: str | Path) -> list[LogSpec]:
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"журнал интеграции: не удалось прочитать {p}: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("logs", []), list):
        raise ConfigError(f"журнал интеграции: {p} должен содержать список logs")

    specs: list[LogSpec] = []
    seen: set[str] = set()
    for i, raw in enumerate(data.get("logs") or [], 1):
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("paths"):
            raise ConfigError(f"журнал №{i}: нужны поля id и paths")
        lid = str(raw["id"])
        if lid in seen:
            raise ConfigError(f"журнал {lid}: повторяющийся id")
        seen.add(lid)
        paths = raw["paths"]
        paths = [paths] if isinstance(paths, str) else list(paths)

        signatures: list[LogSignature] = []
        sig_ids: set[str] = set()
        for j, sraw in enumerate(raw.get("signatures") or [], 1):
            if not isinstance(sraw, dict) or not sraw.get("id") or not sraw.get("pattern"):
                raise ConfigError(f"журнал {lid}, шаблон №{j}: нужны поля id и pattern")
            sid = str(sraw["id"])
            if sid in sig_ids:
                raise ConfigError(f"журнал {lid}: повторяющийся id шаблона {sid}")
            sig_ids.add(sid)
            try:
                regex = re.compile(str(sraw["pattern"]), re.IGNORECASE)
            except re.error as e:
                raise ConfigError(f"журнал {lid}, шаблон {sid}: некорректное регулярное выражение: {e}") from e
            signatures.append(
                LogSignature(sid, str(sraw.get("name") or sid), regex, int(sraw.get("min_count", 1)), float(sraw.get("growth", 3)))
            )
        if not signatures:
            raise ConfigError(f"журнал {lid}: не задано ни одного шаблона signatures")
        specs.append(
            LogSpec(
                id=lid,
                name=str(raw.get("name") or lid),
                paths=tuple(str(x) for x in paths),
                signatures=tuple(signatures),
                encoding=str(raw.get("encoding") or "auto").lower(),
                max_read_mb=float(raw.get("max_read_mb", DEFAULT_MAX_READ_MB)),
            )
        )
    return specs


def expand_paths(spec: LogSpec) -> list[Path]:
    """Файлы, подходящие под пути со звёздочками (например, logs/chz-*.log)."""
    found: set[Path] = set()
    for pattern in spec.paths:
        for match in glob.glob(pattern, recursive=True):
            path = Path(match)
            if path.is_file():
                found.add(path)
    return sorted(found)


def _prefix_hash(path: Path, length: int) -> str:
    with path.open("rb") as f:
        return hashlib.sha1(f.read(min(length, HEAD_BYTES))).hexdigest()


def _decode(chunk: bytes, encoding: str) -> str:
    if encoding == "auto":
        try:
            return chunk.decode("utf-8")
        except UnicodeDecodeError:
            return chunk.decode("cp1251", errors="replace")
    return chunk.decode(encoding, errors="replace")


def _read_new(path: Path, start: int, max_bytes: int) -> tuple[bytes, int, bool]:
    """Дописанные строки с позиции start. Возвращает (данные, новая позиция, были ли пропущены старые байты).

    Данные всегда начинаются с начала строки и заканчиваются концом строки: недописанная последняя строка
    дождётся следующего запуска, а обрезанный «хвост» большого файла не начнётся с середины символа."""
    size = path.stat().st_size
    skipped = size - start > max_bytes
    if skipped:
        start = size - max_bytes
    with path.open("rb") as f:
        mid_line = False
        if start > 0:
            f.seek(start - 1)
            mid_line = f.read(1) != b"\n"
        f.seek(start)
        chunk = f.read(size - start)
    if mid_line:
        first = chunk.find(b"\n")
        if first < 0:
            return b"", start, skipped  # весь кусок внутри одной недописанной строки
        chunk, start = chunk[first + 1 :], start + first + 1
    last = chunk.rfind(b"\n")
    if last < 0:
        return b"", start, skipped  # нет ни одной законченной строки: ждём, пока допишут
    return chunk[: last + 1], start + last + 1, skipped


def evaluate(sig: LogSignature, count: int, history: list[int]) -> tuple[bool, float, int]:
    """(сигнал ли это, среднее за прошлые проверки, порог)."""
    average = sum(history) / len(history) if history else 0.0
    threshold = max(sig.min_count, math.ceil(sig.growth * average))
    return count > 0 and count >= threshold, average, threshold


def scan_log(spec: LogSpec, store: Store) -> MonitorResult:
    result = MonitorResult()
    baselined = store.log_is_baselined(spec.id)
    previous = store.log_files_state(spec.id)
    files = expand_paths(spec)
    if not files:
        result.notes.append(f"{spec.id}: файлов по путям {', '.join(spec.paths)} не найдено")

    counts = {s.id: 0 for s in spec.signatures}
    where: dict[str, set[str]] = {s.id: set() for s in spec.signatures}
    state: dict[str, tuple[int, str]] = {}
    read_bytes = 0

    for path in files:
        key = str(path.resolve())
        try:
            size = path.stat().st_size
            if not baselined:
                state[key] = (size, _prefix_hash(path, size))
                continue
            start = 0
            old = previous.get(key)
            if old is not None:
                offset, head = old
                if size >= offset and _prefix_hash(path, offset) == head:
                    start = offset
                else:
                    result.notes.append(f"{path.name}: файл заменён или обрезан (ротация), читаю с начала")
            data, end, skipped = _read_new(path, start, int(spec.max_read_mb * 1024 * 1024))
            if skipped:
                result.notes.append(f"{path.name}: за раз прочитаны только последние {spec.max_read_mb:.0f} МБ")
            read_bytes += len(data)
            text = _decode(data, spec.encoding)
            for line in text.splitlines():
                for sig in spec.signatures:
                    if sig.regex.search(line):
                        counts[sig.id] += 1
                        where[sig.id].add(path.name)
            state[key] = (end, _prefix_hash(path, end))
        except OSError as e:  # файл занят, удалён во время чтения, нет прав
            result.notes.append(f"{path.name}: не удалось прочитать ({e}); пропускаю до следующего запуска")
            if key in previous:
                state[key] = previous[key]

    if not baselined:
        if state:
            result.notes.append(f"{spec.id}: первый запуск — запомнил позицию в {len(state)} файл(ах), историю не читаю")
    else:
        summary = ", ".join(f"{s.name}: {counts[s.id]}" for s in spec.signatures)
        result.notes.append(f"{spec.id}: прочитано {read_bytes // 1024} КБ в {len(files)} файл(ах); совпадения — {summary}")
        for sig in spec.signatures:
            history = store.log_history(spec.id, sig.id, HISTORY_RUNS)
            hit, average, threshold = evaluate(sig, counts[sig.id], history)
            if not hit:
                continue
            files_text = ", ".join(sorted(where[sig.id])[:5])
            if average > 0:
                message = f"{counts[sig.id]} записей с прошлой проверки при обычных ~{average:.0f} (порог {threshold}); файлы: {files_text}"
            else:
                message = f"{counts[sig.id]} записей с прошлой проверки, раньше не встречалось; файлы: {files_text}"
            result.signals.append(Signal("log", f"{spec.id}:{sig.id}", f"{spec.name}: {sig.name}", "высокая", message))

    if files:  # пока файлов нет, журнал «не начат»: когда пути поправят, существующую историю не примем за новую
        new_counts = counts if baselined else {}
        result.commit = lambda: store.log_save(spec.id, state, new_counts)
    return result


def run_logwatch(specs: list[LogSpec], store: Store) -> MonitorResult:
    """Проверка всех журналов; commit запоминает позиции и счётчики (вызывается после отправки письма)."""
    total = MonitorResult()
    commits = []
    for spec in specs:
        one = scan_log(spec, store)
        total.signals += one.signals
        total.notes += one.notes
        commits.append(one.commit)

    def commit() -> None:
        for c in commits:
            c()

    total.commit = commit
    return total
