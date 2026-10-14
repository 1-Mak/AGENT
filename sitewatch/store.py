"""Состояние между запусками (SQLite): известные документы и здоровье источников."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import DocRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    url           TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    content_hash  TEXT NOT NULL DEFAULT '',
    text          TEXT NOT NULL DEFAULT '',
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    last_changed  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);
CREATE TABLE IF NOT EXISTS sources (
    source_id            TEXT PRIMARY KEY,
    baselined            INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    alerted              INTEGER NOT NULL DEFAULT 0,
    last_ok              TEXT,
    last_error           TEXT
);
CREATE TABLE IF NOT EXISTS probe_state (
    probe_id   TEXT NOT NULL,
    mode       TEXT NOT NULL,
    signature  TEXT NOT NULL DEFAULT '',
    failures   INTEGER NOT NULL DEFAULT 0,
    alerted    INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT,
    PRIMARY KEY (probe_id, mode)
);
CREATE TABLE IF NOT EXISTS log_state (
    log_id      TEXT PRIMARY KEY,
    baselined_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS log_files (
    log_id TEXT NOT NULL,
    path   TEXT NOT NULL,
    offset INTEGER NOT NULL,
    head   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (log_id, path)
);
CREATE TABLE IF NOT EXISTS log_counts (
    log_id       TEXT NOT NULL,
    signature_id TEXT NOT NULL,
    counted_at   TEXT NOT NULL,
    matches      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_counts ON log_counts(log_id, signature_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SourceState:
    baselined: bool = False
    failures: int = 0
    alerted: bool = False


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- документы ---

    def get_document(self, url: str) -> DocRecord | None:
        row = self.conn.execute("SELECT * FROM documents WHERE url = ?", (url,)).fetchone()
        if row is None:
            return None
        return DocRecord(row["url"], row["source_id"], row["title"], row["content_hash"], row["text"])

    def known_urls(self, source_id: str) -> set[str]:
        rows = self.conn.execute("SELECT url FROM documents WHERE source_id = ?", (source_id,))
        return {r["url"] for r in rows}

    def save_records(self, records: Iterable[DocRecord]) -> None:
        now = _now()
        with self.conn:
            for r in records:
                self.conn.execute(
                    """
                    INSERT INTO documents(url, source_id, title, content_hash, text, first_seen, last_seen, last_changed)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        title = excluded.title,
                        content_hash = excluded.content_hash,
                        text = excluded.text,
                        last_seen = excluded.last_seen,
                        last_changed = CASE WHEN documents.content_hash != excluded.content_hash
                                            THEN excluded.last_changed ELSE documents.last_changed END
                    """,
                    (r.url, r.source_id, r.title, r.content_hash, r.text, now, now, now),
                )

    # --- здоровье источников ---

    def source_state(self, source_id: str) -> SourceState:
        row = self.conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
        if row is None:
            return SourceState()
        return SourceState(bool(row["baselined"]), row["consecutive_failures"], bool(row["alerted"]))

    def mark_success(self, source_id: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO sources(source_id, baselined, consecutive_failures, alerted, last_ok, last_error)
                VALUES (?, 1, 0, 0, ?, NULL)
                ON CONFLICT(source_id) DO UPDATE SET
                    baselined = 1, consecutive_failures = 0, alerted = 0, last_ok = excluded.last_ok, last_error = NULL
                """,
                (source_id, _now()),
            )

    def record_failure(self, source_id: str, error: str) -> int:
        """Увеличивает счётчик подряд идущих сбоев и возвращает его новое значение."""
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO sources(source_id, consecutive_failures, last_error) VALUES (?, 1, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    consecutive_failures = consecutive_failures + 1, last_error = excluded.last_error
                """,
                (source_id, error),
            )
        return self.source_state(source_id).failures

    def describe_source(self, source_id: str) -> dict:
        """Сводка для диагностики: что известно об источнике."""
        state = self.conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
        docs = self.conn.execute(
            "SELECT COUNT(*) AS n, MAX(last_changed) AS last_changed FROM documents WHERE source_id = ?", (source_id,)
        ).fetchone()
        return {
            "baselined": bool(state and state["baselined"]),
            "failures": state["consecutive_failures"] if state else 0,
            "last_ok": state["last_ok"] if state else None,
            "last_error": state["last_error"] if state else None,
            "documents": docs["n"],
            "last_changed": docs["last_changed"],
        }

    def known_sources(self) -> dict[str, int]:
        """Какие источники есть в памяти и сколько документов о каждом известно."""
        found = {r["source_id"]: 0 for r in self.conn.execute("SELECT source_id FROM sources")}
        for r in self.conn.execute("SELECT source_id, COUNT(*) AS n FROM documents GROUP BY source_id"):
            found[r["source_id"]] = r["n"]
        return found

    def reset(self, source_ids: list[str] | None = None) -> tuple[int, int]:
        """Забывает документы и состояние источников (все или только перечисленные).
        Возвращает (удалено документов, сброшено источников)."""
        with self.conn:
            if source_ids:
                marks = ",".join("?" * len(source_ids))  # только заполнители, значения передаются отдельно
                docs = self.conn.execute(f"DELETE FROM documents WHERE source_id IN ({marks})", source_ids).rowcount
                sources = self.conn.execute(f"DELETE FROM sources WHERE source_id IN ({marks})", source_ids).rowcount
            else:
                docs = self.conn.execute("DELETE FROM documents").rowcount
                sources = self.conn.execute("DELETE FROM sources").rowcount
        return docs, sources

    def mark_alerted(self, source_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE sources SET alerted = 1 WHERE source_id = ?", (source_id,))

    # --- контрольные вызовы API (probes) ---

    def probe_get(self, probe_id: str, mode: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM probe_state WHERE probe_id = ? AND mode = ?", (probe_id, mode)).fetchone()
        if row is None:
            return None
        return {"signature": row["signature"], "failures": row["failures"], "alerted": bool(row["alerted"])}

    def probe_save(self, probe_id: str, mode: str, signature: str | None, failures: int = 0, alerted: bool = False) -> None:
        """signature=None — оставить прежний отпечаток (проба не ответила, обновлять базу нечем)."""
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO probe_state(probe_id, mode, signature, failures, alerted, checked_at)
                VALUES (?, ?, COALESCE(?, ''), ?, ?, ?)
                ON CONFLICT(probe_id, mode) DO UPDATE SET
                    signature = COALESCE(?, probe_state.signature),
                    failures = excluded.failures, alerted = excluded.alerted, checked_at = excluded.checked_at
                """,
                (probe_id, mode, signature, failures, int(alerted), _now(), signature),
            )

    # --- журнал интеграции (logwatch) ---

    def log_is_baselined(self, log_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM log_state WHERE log_id = ?", (log_id,)).fetchone() is not None

    def log_files_state(self, log_id: str) -> dict[str, tuple[int, str]]:
        rows = self.conn.execute("SELECT path, offset, head FROM log_files WHERE log_id = ?", (log_id,))
        return {r["path"]: (r["offset"], r["head"]) for r in rows}

    def log_history(self, log_id: str, signature_id: str, limit: int = 7) -> list[int]:
        """Сколько совпадений было в последних проверках (от свежих к старым)."""
        rows = self.conn.execute(
            "SELECT matches FROM log_counts WHERE log_id = ? AND signature_id = ? ORDER BY rowid DESC LIMIT ?",
            (log_id, signature_id, limit),
        )
        return [r["matches"] for r in rows]

    def log_save(self, log_id: str, files: dict[str, tuple[int, str]], counts: dict[str, int]) -> None:
        now = _now()
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO log_state(log_id, baselined_at) VALUES (?, ?)", (log_id, now))
            for path, (offset, head) in files.items():
                self.conn.execute(
                    """
                    INSERT INTO log_files(log_id, path, offset, head) VALUES (?, ?, ?, ?)
                    ON CONFLICT(log_id, path) DO UPDATE SET offset = excluded.offset, head = excluded.head
                    """,
                    (log_id, path, offset, head),
                )
            for signature_id, matches in counts.items():
                self.conn.execute(
                    "INSERT INTO log_counts(log_id, signature_id, counted_at, matches) VALUES (?, ?, ?, ?)",
                    (log_id, signature_id, now, matches),
                )
            # хранить историю дольше нескольких недель незачем
            self.conn.execute(
                "DELETE FROM log_counts WHERE log_id = ? AND rowid NOT IN "
                "(SELECT rowid FROM log_counts WHERE log_id = ? ORDER BY rowid DESC LIMIT 2000)",
                (log_id, log_id),
            )

    def monitor_state_count(self) -> int:
        """Сколько записей о пробах API и журналах хранится (для сброса)."""
        total = 0
        for table in ("probe_state", "log_state"):
            total += self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # имена таблиц наши, не пользовательские
        return total

    def reset_monitors(self) -> int:
        """Забывает отпечатки проб и позиции в журналах: следующий запуск снова запомнит их молча."""
        count = self.monitor_state_count()
        with self.conn:
            for table in ("probe_state", "log_state", "log_files", "log_counts"):
                self.conn.execute(f"DELETE FROM {table}")
        return count
