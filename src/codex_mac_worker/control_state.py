from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


def operation_id(action: str, target: str, expected_head: str) -> str:
    raw = f"v1\0{action}\0{target}\0{expected_head}".encode()
    return hashlib.sha256(raw).hexdigest()


class ControlState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                expected_head TEXT NOT NULL,
                state TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            )"""
        )
        self.connection.commit()

    @property
    def journal_mode(self) -> str:
        row = self.connection.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        return str(row[0]).lower()

    def begin(self, key: str, action: str, target: str, expected_head: str) -> bool:
        created = datetime.now(UTC).isoformat()
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO operations VALUES (?, ?, ?, ?, 'started', NULL, ?, NULL)",
            (key, action, target, expected_head, created),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def complete(self, key: str, result: dict[str, Any]) -> None:
        self.connection.execute(
            "UPDATE operations SET state='completed', result_json=?, completed_at=? "
            "WHERE operation_id=?",
            (json.dumps(result, sort_keys=True), datetime.now(UTC).isoformat(), key),
        )
        self.connection.commit()

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM operations WHERE operation_id=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["result"] = json.loads(item.pop("result_json")) if item["result_json"] else None
        return item

    def close(self) -> None:
        self.connection.close()

