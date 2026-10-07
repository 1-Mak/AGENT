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

    def mark_alerted(self, source_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE sources SET alerted = 1 WHERE source_id = ?", (source_id,))
