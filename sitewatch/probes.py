"""Контрольные вызовы API: раз в сутки дёргаем несколько реальных методов интеграции и ищем признаки скорых изменений.

Что считается сигналом (сравнение с прошлым запуском, первый запуск молча запоминает «как сейчас»):
  - HTTP 410 — метод отключён (сигнал даже при первом запуске);
  - запрос стал перенаправляться на другой хост;
  - изменился статус ответа (например, 401 -> 404);
  - при том же статусе (401, 404 и т.д.) изменился текст ошибки;
  - при том же статусе изменился состав полей JSON-ответа;
  - метод не отвечает несколько запусков подряд.

Проба ничего не меняет в системе: это обычные запросы, лучше на тестовом контуре и с тестовыми данными.
Токен в письмо и журнал не попадает.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

import requests
import yaml

from .config import ConfigError
from .signals import MonitorResult, Signal
from .store import Store

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_000_000
MAX_SCHEMA_PATHS = 300
MAX_MAP_KEYS = 50  # словарь с большим числом ключей — это набор данных, а не схема
DEFAULT_UNREACHABLE_AFTER = 2
VALID_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
ERROR_KEYS = (
    "error_message", "errormessage", "error_description", "message", "error", "detail", "description", "reason",
    "title", "code", "error_code", "errorcode",
)  # fmt: skip
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")  # ${ИМЯ} или ${ИМЯ:-значение по умолчанию}
_MASKS = [
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<uuid>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<время>"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<hex>"),
    (re.compile(r"\d{5,}"), "<число>"),
]


@dataclass(frozen=True)
class Probe:
    id: str
    name: str
    url: str
    method: str = "GET"
    headers: tuple[tuple[str, str], ...] = ()
    json_body: object | None = None
    body: str | None = None
    timeout: float = 20.0


@dataclass(frozen=True)
class ProbeConfig:
    probes: tuple[Probe, ...]
    token_var: str = "TRUE_API_TOKEN"  # переменная, в которой лежит токен (из .env)
    token_command: tuple[str, ...] = ()  # необязательно: команда, которая печатает токен (подпись УКЭП и т.п.)
    unreachable_after: int = DEFAULT_UNREACHABLE_AFTER


def load_probes(path: str | Path) -> ProbeConfig:
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"пробы API: не удалось прочитать {p}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"пробы API: {p} должен содержать словарь")

    raw_probes = data.get("probes") or []
    if not isinstance(raw_probes, list):
        raise ConfigError(f"пробы API: в {p} probes должен быть списком")
    probes: list[Probe] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_probes, 1):
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("url"):
            raise ConfigError(f"проба №{i}: нужны поля id и url")
        pid = str(raw["id"])
        if pid in seen:
            raise ConfigError(f"проба {pid}: повторяющийся id")
        seen.add(pid)
        method = str(raw.get("method") or "GET").upper()
        if method not in VALID_METHODS:
            raise ConfigError(f"проба {pid}: неизвестный method {method!r}")
        headers = raw.get("headers") or {}
        if not isinstance(headers, dict):
            raise ConfigError(f"проба {pid}: headers должен быть словарём")
        probes.append(
            Probe(
                id=pid,
                name=str(raw.get("name") or pid),
                url=str(raw["url"]),
                method=method,
                headers=tuple((str(k), str(v)) for k, v in headers.items()),
                json_body=raw.get("json"),
                body=None if raw.get("body") is None else str(raw["body"]),
                timeout=float(raw.get("timeout", 20)),
            )
        )

    token = data.get("token") or {}
    if not isinstance(token, dict):
        raise ConfigError(f"пробы API: token в {p} должен быть словарём (env, command)")
    command = token.get("command") or ()
    if isinstance(command, str):
        command = shlex.split(command, posix=os.name != "nt")
    return ProbeConfig(
        probes=tuple(probes),
        token_var=str(token.get("env") or "TRUE_API_TOKEN"),
        token_command=tuple(str(c) for c in command),
        unreachable_after=int(data.get("unreachable_after", DEFAULT_UNREACHABLE_AFTER)),
    )


# --- подготовка запроса ---


def _substitute(value, variables: Mapping[str, str], missing: list[str]):
    """Подставляет ${ИМЯ} во все строки (в том числе вложенные в JSON). Не найденные имена складывает в missing."""
    if isinstance(value, str):

        def repl(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if variables.get(name):
                return variables[name]
            if default is not None:
                return default
            missing.append(name)
            return m.group(0)

        return _VAR.sub(repl, value)
    if isinstance(value, list):
        return [_substitute(v, variables, missing) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, variables, missing) for k, v in value.items()}
    return value


@dataclass
class Prepared:
    probe: Probe
    url: str
    headers: dict[str, str]
    json_body: object | None
    body: str | None
    mode: str  # "full" — все заголовки на месте; "anon" — часть заголовков (токен) пропущена

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc.lower()


def prepare(probe: Probe, variables: Mapping[str, str]) -> Prepared | str:
    """Запрос с подставленными переменными либо текст причины, почему пробу пропускаем."""
    missing: list[str] = []
    url = _substitute(probe.url, variables, missing)
    json_body = _substitute(probe.json_body, variables, missing)
    body = _substitute(probe.body, variables, missing) if probe.body is not None else None
    if missing:
        return f"не задано: {', '.join(dict.fromkeys(missing))} (файл .env) — проба пропущена"
    headers: dict[str, str] = {}
    mode = "full"
    for name, raw in probe.headers:
        gaps: list[str] = []
        value = _substitute(raw, variables, gaps)
        if gaps:
            mode = "anon"  # например, нет токена: проверяем метод без авторизации, это тоже полезно
        else:
            headers[name] = value
    return Prepared(probe, url, headers, json_body, body, mode)


# --- разбор ответа ---


def _mask(text: str) -> str:
    for rx, repl in _MASKS:
        text = rx.sub(repl, text)
    return " ".join(text.split())


def _error_strings(data, found: list[str], depth: int = 0) -> None:
    if depth > 4 or len(found) >= 6:
        return
    if isinstance(data, dict):
        for key, value in data.items():
            if str(key).lower() in ERROR_KEYS and isinstance(value, (str, int)) and not isinstance(value, bool):
                found.append(str(value))
            elif isinstance(value, (dict, list)):
                _error_strings(value, found, depth + 1)
    elif isinstance(data, list):
        for item in data[:5]:
            _error_strings(item, found, depth + 1)


def error_text(body: str, data) -> str:
    """Текст ошибки без изменчивых частей (номера, время, идентификаторы), чтобы сравнивать суть."""
    if data is not None:
        found: list[str] = []
        _error_strings(data, found)
        return _mask(" | ".join(found))[:300]
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.DOTALL | re.IGNORECASE)
    return _mask(re.sub(r"<[^>]+>", " ", text))[:300]


def schema_paths(data, prefix: str = "", out: set[str] | None = None) -> set[str]:
    """Пути полей JSON: a, a.b, a[].c. Значения не сравниваем — они меняются от вызова к вызову."""
    out = set() if out is None else out
    if len(out) >= MAX_SCHEMA_PATHS:
        return out
    if isinstance(data, dict):
        if len(data) > MAX_MAP_KEYS:
            out.add(f"{prefix}.*" if prefix else "*")
            return out
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.add(path)
            schema_paths(value, path, out)
    elif isinstance(data, list):
        for item in data[:50]:
            schema_paths(item, f"{prefix}[]", out)
    return out


_QUERY = re.compile(r"\?[^\s)'\"]*")


def _without_query(text: str) -> str:
    """В тексте сетевой ошибки бывает адрес вместе с параметрами запроса, а в них — ключи: оставляем только путь."""
    return _QUERY.sub("?…", text)


def _decode_body(raw: bytes, content_type: str) -> str:
    """UTF-8; если не вышло — кодировка из заголовка, а без неё cp1251 (старые серверы на Windows)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
        try:
            return raw.decode(match.group(1) if match else "cp1251", errors="replace")
        except LookupError:
            return raw.decode("cp1251", errors="replace")


def call(prepared: Prepared, session: requests.Session) -> dict:
    """Один запрос. Возвращает отпечаток ответа; ключ failure заполнен, если ответа по сути нет (сеть, 5xx, 429)."""
    sig: dict = {"status": None, "host": prepared.host, "location_host": "", "error": "", "schema": []}
    try:
        resp = session.request(
            prepared.probe.method,
            prepared.url,
            headers=prepared.headers,
            json=prepared.json_body,
            data=prepared.body,
            timeout=prepared.probe.timeout,
            allow_redirects=False,  # перенаправление на другой хост — само по себе сигнал, идти за ним не нужно
            stream=True,
        )
    except requests.RequestException as e:
        sig["failure"] = f"{type(e).__name__}: {_without_query(str(e))[:160]}"
        return sig
    try:
        raw = b""
        for chunk in resp.iter_content(65536):
            raw += chunk
            if len(raw) >= MAX_BODY_BYTES:
                break
    except requests.RequestException as e:
        sig["failure"] = f"ответ оборван: {type(e).__name__}"
        return sig
    finally:
        resp.close()

    sig["status"] = resp.status_code
    body = _decode_body(raw, resp.headers.get("Content-Type", ""))
    try:
        data = json.loads(body) if body.strip() else None
    except ValueError:
        data = None
    location = resp.headers.get("Location", "")
    if 300 <= resp.status_code < 400 and location:
        sig["location_host"] = urlsplit(location).netloc.lower()
    if resp.status_code >= 400 or data is None:
        sig["error"] = error_text(body, data)
    if isinstance(data, (dict, list)):
        sig["schema"] = sorted(schema_paths(data))
    if resp.status_code in TRANSIENT_STATUSES:
        sig["failure"] = f"HTTP {resp.status_code}"
    return sig


# --- сравнение с прошлым запуском ---


def _short(items, limit: int = 8) -> str:
    items = list(items)
    more = f" и ещё {len(items) - limit}" if len(items) > limit else ""
    return ", ".join(items[:limit]) + more


def compare(prev: dict | None, cur: dict) -> list[tuple[str, str]]:
    """Список (уровень, сообщение) по двум отпечаткам. prev=None — первый запуск."""
    out: list[tuple[str, str]] = []
    status = cur["status"]
    if status == 410 and (prev is None or prev.get("status") != 410):
        out.append(("высокая", "HTTP 410 Gone: метод отключён"))
    if prev is None:
        return out
    if prev.get("host") != cur["host"]:
        return out  # адрес в настройках пробы поменяли сами — это не изменение на стороне API

    if cur["location_host"] != prev.get("location_host", ""):
        was = prev.get("location_host") or "без перенаправления"
        now = cur["location_host"] or "перенаправления больше нет"
        out.append(("высокая", f"запрос перенаправляется на другой хост: {now} (раньше: {was})"))

    if status != prev.get("status"):
        if status != 410:
            tail = f"; текст ошибки: «{cur['error'][:120]}»" if cur["error"] else ""
            level = "высокая" if status >= 300 else "средняя"
            out.append((level, f"статус ответа изменился: HTTP {prev.get('status')} → HTTP {status}{tail}"))
        return out  # поля и текст при другом статусе несравнимы

    if status >= 300 and cur["error"] != prev.get("error", ""):
        out.append(
            ("средняя", f"новый текст ошибки (HTTP {status}): «{cur['error'][:160]}» (раньше: «{prev.get('error', '')[:160]}»)")
        )
    old_fields, new_fields = set(prev.get("schema", [])), set(cur["schema"])
    if old_fields - new_fields:
        out.append(("высокая", f"в ответе пропали поля: {_short(sorted(old_fields - new_fields))}"))
    if new_fields - old_fields:
        out.append(("низкая", f"в ответе появились поля: {_short(sorted(new_fields - old_fields))}"))
    return out


def describe(sig: dict) -> str:
    """Одна строка для журнала и команды probes."""
    if sig.get("failure"):
        return f"нет ответа ({sig['failure']})"
    parts = [f"HTTP {sig['status']}"]
    if sig["location_host"]:
        parts.append(f"перенаправление на {sig['location_host']}")
    if sig["error"]:
        parts.append(f"текст: «{sig['error'][:100]}»")
    if sig["schema"]:
        parts.append(f"полей в ответе: {len(sig['schema'])}")
    return ", ".join(parts)


# --- запуск ---


def fetch_token(config: ProbeConfig, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> tuple[str, str]:
    """Токен из команды token.command. Возвращает (токен, пояснение, если не получилось)."""
    if not config.token_command:
        return "", ""
    try:
        done = runner(list(config.token_command), capture_output=True, text=True, encoding="utf-8", timeout=90, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return "", f"команда получения токена не запустилась ({type(e).__name__}: {e})"
    token = (done.stdout or "").strip().splitlines()[-1].strip() if (done.stdout or "").strip() else ""
    if done.returncode != 0 or not token:
        return "", f"команда получения токена завершилась с кодом {done.returncode} и не вернула токен"
    return token, ""


def run_probes(
    config: ProbeConfig,
    store: Store,
    *,
    session: requests.Session | None = None,
    env: Mapping[str, str] | None = None,
    token_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> MonitorResult:
    """Выполняет пробы и сравнивает с запомненным. Новое состояние записывается в commit (после отправки письма)."""
    session = session or requests.Session()
    variables = dict(os.environ if env is None else env)
    result = MonitorResult()

    if config.token_command and not variables.get(config.token_var):
        token, problem = fetch_token(config, token_runner)
        if token:
            variables[config.token_var] = token
            result.notes.append("токен получен командой из probes.yaml")
        else:
            result.notes.append(f"{problem}; пробы идут без токена")

    saves: list[Callable[[], None]] = []
    for probe in config.probes:
        prepared = prepare(probe, variables)
        if isinstance(prepared, str):
            result.notes.append(f"{probe.id}: {prepared}")
            continue
        sig = call(prepared, session)
        prev_state = store.probe_get(probe.id, prepared.mode)
        no_token = " (токен не задан, проверка без него)" if prepared.mode == "anon" else ""
        result.notes.append(f"{probe.id}{no_token}: {describe(sig)}")

        failure = sig.pop("failure", "")
        if failure:
            failures = (prev_state["failures"] if prev_state else 0) + 1
            alerted = bool(prev_state and prev_state["alerted"])
            if failures >= config.unreachable_after and not alerted:
                result.signals.append(
                    Signal("probe", probe.id, probe.name, "средняя", f"нет нормального ответа {failures} запуска(ов) подряд: {failure}")
                )
                alerted = True
            saves.append(lambda p=probe.id, m=prepared.mode, f=failures, a=alerted: store.probe_save(p, m, None, f, a))
            continue

        prev_sig = json.loads(prev_state["signature"]) if prev_state and prev_state["signature"] else None
        for level, message in compare(prev_sig, sig):
            result.signals.append(Signal("probe", probe.id, probe.name, level, message))
        if prev_sig is None:
            result.notes.append(f"{probe.id}: первый запуск — состояние запомнено как исходное")
        saves.append(
            lambda p=probe.id, m=prepared.mode, s=json.dumps(sig, ensure_ascii=False, sort_keys=True): store.probe_save(p, m, s)
        )

    def commit() -> None:
        for save in saves:
            save()

    result.commit = commit
    return result
