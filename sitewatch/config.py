"""Настройки (из окружения) и описание источников (из YAML)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .extract import normalize_url
from .models import Source

VALID_TYPES = {"page", "list", "sitemap", "pdf"}


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
                initial_notify=int(raw.get("initial_notify", 1 if stype in ("list", "sitemap") else 0)),
                max_new_per_run=int(raw.get("max_new_per_run", 10)),
            )
        )
    return sources
