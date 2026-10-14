"""Состояние между запусками (SQLite): известные документы и здоровье источников."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import Deadline, DocRecord

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
CREATE TABLE IF NOT EXISTS deadlines (
    key         TEXT PRIMARY KEY,
    due         TEXT NOT NULL,
    title       TEXT NOT NULL,
    origin      TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    approx      INTEGER NOT NULL DEFAULT 0,
    components  TEXT NOT NULL DEFAULT '[]',
    note        TEXT NOT NULL DEFAULT '',
    manual      INTEGER NOT NULL DEFAULT 0,
    reminded_at INTEGER,
    dismissed   INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL
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

    # --- реестр сроков ---

    def load_deadlines(self) -> list[Deadline]:
        rows = self.conn.execute("SELECT * FROM deadlines ORDER BY due, title")
        return [
            Deadline(
                due=date.fromisoformat(r["due"]),
                title=r["title"],
                origin=r["origin"],
                url=r["url"],
                approx=bool(r["approx"]),
                components=tuple(json.loads(r["components"])),
                note=r["note"],
                manual=bool(r["manual"]),
                reminded_at=r["reminded_at"],
                dismissed=bool(r["dismissed"]),
            )
            for r in rows
        ]

    def save_deadlines(self, deadlines: Iterable[Deadline]) -> None:
        """Добавляет сроки; у уже известных обновляет описание, но не трогает «напомнено» и «снято»."""
        now = _now()
        with self.conn:
            for d in deadlines:
                self.conn.execute(
                    """
                    INSERT INTO deadlines(key, due, title, origin, url, approx, components, note, manual, first_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        origin = excluded.origin, url = excluded.url, approx = excluded.approx,
                        components = excluded.components, note = excluded.note
                    """,
                    (
                        d.key, d.due.isoformat(), d.title, d.origin, d.url, int(d.approx),
                        json.dumps(list(d.components), ensure_ascii=False), d.note, int(d.manual), now,
                    ),
                )

    def sync_manual_deadlines(self, current: Iterable[Deadline]) -> int:
        """Ручные сроки в базе приводятся к файлу milestones.yaml: удалённые из файла убираются. Возвращает, сколько убрано."""
        current = list(current)
        keep = {d.key for d in current}
        self.save_deadlines(current)
        with self.conn:
            stale = [r["key"] for r in self.conn.execute("SELECT key FROM deadlines WHERE manual = 1") if r["key"] not in keep]
            for key in stale:
                self.conn.execute("DELETE FROM deadlines WHERE key = ?", (key,))
        return len(stale)

    def mark_reminded(self, thresholds: dict[str, int]) -> None:
        with self.conn:
            for key, threshold in thresholds.items():
                self.conn.execute("UPDATE deadlines SET reminded_at = ? WHERE key = ?", (threshold, key))

    def dismiss_deadline(self, key_prefix: str) -> list[str]:
        """Снимает срок с календаря (напоминания прекращаются). Ключ можно писать начало. Возвращает снятые заголовки."""
        rows = self.conn.execute("SELECT key, title FROM deadlines WHERE key LIKE ?", (key_prefix.strip() + "%",)).fetchall()
        with self.conn:
            for r in rows:
                self.conn.execute("UPDATE deadlines SET dismissed = 1 WHERE key = ?", (r["key"],))
        return [r["title"] for r in rows]

    def mark_alerted(self, source_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE sources SET alerted = 1 WHERE source_id = ?", (source_id,))
