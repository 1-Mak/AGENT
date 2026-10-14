"""Настройки (из окружения) и описание источников (из YAML)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import yaml

from .extract import normalize_url
from .models import Deadline, GlossaryEntry, Scope, Source

VALID_TYPES = {"page", "list", "sitemap", "pdf", "text", "telegram"}
VALID_ROBOTS = {"", "respect", "warn", "off"}
VALID_FETCH = {"http", "browser"}


class ConfigError(Exception):
    pass


_loaded_from_file: set[str] = set()  # ключи, которые в окружение положили мы, а не пользователь


def load_dotenv(path: str | Path = ".env") -> None:
    """Минимальный загрузчик .env.

    Настоящие переменные окружения главнее файла. Значения, которые ранее подставили из файла,
    обновляются при повторной загрузке — так правки .env подхватываются без перезапуска меню.
    """
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key not in os.environ or key in _loaded_from_file:
            os.environ[key] = value
            _loaded_from_file.add(key)


@dataclass(frozen=True)
class Settings:
    db_path: str = "data/state.db"
    user_agent: str = "Mozilla/5.0 (compatible; sitewatch/0.1)"
    request_timeout: float = 30.0
    request_delay: float = 1.0
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    llm_thinking: bool = False  # режим размышлений: для пересказа не нужен, а токены платные
    max_doc_chars: int = 120_000
    max_pdf_mb: float = 50.0  # файлы больше этого размера не скачиваются
    browser: str = ""  # какой браузер открывает источники с fetch: browser: msedge | chrome | chromium; пусто — подобрать
    robots_mode: str = "respect"  # respect — не ходить туда, где запрещает robots.txt; warn — только предупреждать; off
    failure_alert_threshold: int = 3
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_security: str = ""  # ssl | starttls | none; пусто — выбирается по порту
    mail_from: str = ""
    mail_to: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        smtp_user = env.get("SMTP_USER", "")
        return cls(
            db_path=env.get("SITEWATCH_DB", cls.db_path),
            user_agent=env.get("SITEWATCH_USER_AGENT", cls.user_agent),
            request_delay=float(env.get("SITEWATCH_REQUEST_DELAY", cls.request_delay)),
            max_pdf_mb=float(env.get("SITEWATCH_MAX_PDF_MB", cls.max_pdf_mb)),
            browser=env.get("SITEWATCH_BROWSER", "").strip().lower(),
            robots_mode=env.get("SITEWATCH_ROBOTS", cls.robots_mode).strip().lower() or cls.robots_mode,
            llm_api_key=env.get("DEEPSEEK_API_KEY") or env.get("LLM_API_KEY", ""),
            llm_base_url=env.get("LLM_BASE_URL", cls.llm_base_url).rstrip("/"),
            llm_model=env.get("LLM_MODEL", cls.llm_model),
            llm_thinking=env.get("LLM_THINKING", "off").strip().lower() in ("on", "1", "true", "yes"),
            smtp_host=env.get("SMTP_HOST", ""),
            smtp_port=int(env.get("SMTP_PORT", cls.smtp_port)),
            smtp_user=smtp_user,
            smtp_password=env.get("SMTP_PASSWORD", ""),
            smtp_security=env.get("SMTP_SECURITY", "").lower(),
            mail_from=env.get("MAIL_FROM", "") or smtp_user,
            mail_to=tuple(a.strip() for a in env.get("MAIL_TO", "").split(",") if a.strip()),
        )

    def missing_mail_settings(self) -> list[str]:
        missing = []
        if not self.smtp_host:
            missing.append("SMTP_HOST")
        if not self.mail_from:
            missing.append("MAIL_FROM")
        if not self.mail_to:
            missing.append("MAIL_TO")
        return missing


def load_sources(path: str | Path) -> list[Source]:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"Файл конфигурации не найден: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Ошибка YAML в {p}: {e}") from e

    raw_sources = data.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError(f"В {p} нет списка sources")

    sources: list[Source] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(raw_sources, 1):
        where = f"источник №{i}"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: ожидается словарь")
        for key in ("id", "type", "url"):
            if not raw.get(key):
                raise ConfigError(f"{where}: не задано поле {key}")
        sid = str(raw["id"])
        where = f"источник {sid}"
        if sid in seen_ids:
            raise ConfigError(f"{where}: повторяющийся id")
        seen_ids.add(sid)

        stype = raw["type"]
        if stype not in VALID_TYPES:
            raise ConfigError(f"{where}: type должен быть один из {sorted(VALID_TYPES)}")

        pattern = raw.get("link_pattern")
        if stype in ("list", "sitemap") and not pattern:
            raise ConfigError(f"{where}: для type={stype} нужен link_pattern")
        ignore = tuple(raw.get("ignore_regex") or ())
        for rx in (pattern, *ignore):
            if rx:
                try:
                    re.compile(rx)
                except re.error as e:
                    raise ConfigError(f"{where}: некорректное регулярное выражение {rx!r}: {e}") from e

        robots = str(raw.get("robots") or "").lower()
        if robots not in VALID_ROBOTS:
            raise ConfigError(f"{where}: robots должен быть respect, warn или off (получено: {robots!r})")

        fetch = str(raw.get("fetch") or "http").lower()
        if fetch not in VALID_FETCH:
            raise ConfigError(f"{where}: fetch должен быть http или browser (получено: {fetch!r})")
        if fetch == "browser" and raw["url"].lower().startswith("file:"):
            raise ConfigError(f"{where}: fetch: browser не нужен для локальных файлов (file://)")

        sources.append(
            Source(
                id=sid,
                name=str(raw.get("name") or sid),
                type=stype,
                url=normalize_url(str(raw["url"])),
                focus=" ".join(str(raw.get("focus") or "").split()),
                link_pattern=pattern,
                content_selector=raw.get("content_selector"),
                ignore_regex=ignore,
                recheck_latest=int(raw.get("recheck_latest", 0)),
                initial_notify=int(raw.get("initial_notify", 1 if stype in ("list", "sitemap", "telegram") else 0)),
                max_new_per_run=int(raw.get("max_new_per_run", 10)),
                enabled=bool(raw.get("enabled", True)),
                robots=robots,
                fetch=fetch,
                strip_query=bool(raw.get("strip_query", False)),
            )
        )
    return sources


def _read_yaml(path: Path, what: str) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"{what}: не удалось прочитать {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{what}: {path} должен содержать словарь")
    return data


def load_glossary(path: Path) -> tuple[GlossaryEntry, ...]:
    entries = []
    for i, raw in enumerate(_read_yaml(path, "глоссарий").get("entries") or [], 1):
        if not isinstance(raw, dict) or not raw.get("name"):
            raise ConfigError(f"глоссарий {path}: запись №{i} без поля name")
        entries.append(
            GlossaryEntry(
                name=str(raw["name"]).strip(),
                terms=tuple(str(t).strip() for t in (raw.get("terms") or []) if str(t).strip()),
                where=" ".join(str(raw.get("where") or "").split()),
            )
        )
    return tuple(entries)


def _parse_date(value, where: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ConfigError(f"{where}: не понимаю дату {text!r}, нужен вид ДД.ММ.ГГГГ")


def load_milestones(path: Path) -> tuple[Deadline, ...]:
    """Сроки, которые вы знаете сами (из рассылки ЧЗ, письма клиента): config/milestones.yaml."""
    items = []
    for i, raw in enumerate(_read_yaml(path, "календарь сроков").get("milestones") or [], 1):
        where = f"календарь сроков {path}: запись №{i}"
        if not isinstance(raw, dict) or not raw.get("title") or not raw.get("date"):
            raise ConfigError(f"{where}: нужны поля date и title")
        components = raw.get("components") or []
        if isinstance(components, str):
            components = [components]
        items.append(
            Deadline(
                due=_parse_date(raw["date"], where),
                title=" ".join(str(raw["title"]).split()),
                origin=" ".join(str(raw.get("origin") or "вручную").split()),
                url=str(raw.get("url") or ""),
                approx=bool(raw.get("approx", False)),
                components=tuple(str(c).strip() for c in components if str(c).strip()),
                note=" ".join(str(raw.get("note") or "").split()),
                manual=True,
            )
        )
    return tuple(items)


def _resolve_file(config: Path, value: str, what: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        found = candidate
    else:
        # сначала рядом с файлом источников, потом от рабочей папки (так пишут и «glossary.yaml», и «config/glossary.yaml»)
        options = [config.parent / candidate, Path.cwd() / candidate]
        found = next((c for c in options if c.is_file()), options[0])
    if not found.is_file():
        raise ConfigError(f"{what}: файл не найден: {value}")
    return found


def load_scope(path: str | Path) -> Scope:
    """Блок scope из файла источников: тема, которая нужна читателю, и таблица «что где менять»."""
    p = Path(path)
    block = _read_yaml(p, "конфигурация").get("scope") or {}
    if not isinstance(block, dict):
        raise ConfigError(f"{p}: scope должен быть словарём")
    glossary: tuple[GlossaryEntry, ...] = ()
    if block.get("glossary"):
        glossary = load_glossary(_resolve_file(p, str(block["glossary"]), "scope.glossary"))
    milestones: tuple[Deadline, ...] = ()
    if block.get("milestones"):
        milestones = load_milestones(_resolve_file(p, str(block["milestones"]), "scope.milestones"))
    return Scope(
        interests=" ".join(str(block.get("interests") or "").split()),
        include_general=bool(block.get("include_general", True)),
        glossary=glossary,
        milestones=milestones,
    )
