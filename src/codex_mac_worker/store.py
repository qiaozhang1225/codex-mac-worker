from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class EventStore:
    """Durable task state and transactional outbox for one local worker."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    @property
    def journal_mode(self) -> str:
        row = self.connection.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                repo TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                task_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                branch TEXT,
                worktree TEXT,
                session_id TEXT,
                pr_number INTEGER,
                claimed_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (repo, issue_number)
            );

            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                attempt INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                exit_code INTEGER,
                result_json TEXT,
                UNIQUE (repo, issue_number, attempt)
            );

            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                issue_number INTEGER,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                action TEXT NOT NULL,
                author TEXT NOT NULL,
                created_at TEXT NOT NULL,
                executed_at TEXT,
                result TEXT
            );

            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                remote_id TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                failed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS worker_state (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS delivery_checkpoints (
                repo TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                task_hash TEXT NOT NULL,
                branch TEXT NOT NULL,
                worktree TEXT NOT NULL,
                context_commit TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                task_commit_sha TEXT NOT NULL,
                integrated_base_sha TEXT NOT NULL,
                integration_refreshes INTEGER NOT NULL DEFAULT 0,
                project_config_hash TEXT NOT NULL,
                verification_profile TEXT NOT NULL,
                verification_commands_json TEXT NOT NULL,
                verification_result_json TEXT NOT NULL,
                structured_result_json TEXT NOT NULL,
                model TEXT,
                cli_version TEXT,
                session_id TEXT,
                phase TEXT NOT NULL DEFAULT 'checkpointed',
                retryable INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (repo, issue_number, task_hash)
            );
            """
        )
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(outbox)").fetchall()
        }
        for name, definition in (
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("last_error", "TEXT"),
            ("failed_at", "TEXT"),
        ):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE outbox ADD COLUMN {name} {definition}")
        checkpoint_columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(delivery_checkpoints)"
            ).fetchall()
        }
        for name, definition in (
            ("task_commit_sha", "TEXT"),
            ("integrated_base_sha", "TEXT"),
            ("integration_refreshes", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in checkpoint_columns:
                self.connection.execute(
                    f"ALTER TABLE delivery_checkpoints ADD COLUMN {name} {definition}"
                )
        self.connection.execute(
            """
            UPDATE delivery_checkpoints
            SET task_commit_sha=COALESCE(task_commit_sha, commit_sha),
                integrated_base_sha=COALESCE(integrated_base_sha, context_commit),
                integration_refreshes=COALESCE(integration_refreshes, 0)
            """
        )
        self.connection.commit()

    def upsert_task(
        self,
        *,
        repo: str,
        issue_number: int,
        task_hash: str,
        state: str,
        branch: str | None = None,
        worktree: str | None = None,
        session_id: str | None = None,
        pr_number: int | None = None,
    ) -> None:
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO tasks (
                repo, issue_number, task_hash, state, branch, worktree,
                session_id, pr_number, claimed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo, issue_number) DO UPDATE SET
                task_hash=excluded.task_hash,
                state=excluded.state,
                branch=COALESCE(excluded.branch, tasks.branch),
                worktree=COALESCE(excluded.worktree, tasks.worktree),
                session_id=COALESCE(excluded.session_id, tasks.session_id),
                pr_number=COALESCE(excluded.pr_number, tasks.pr_number),
                updated_at=excluded.updated_at
            """,
            (
                repo,
                issue_number,
                task_hash,
                state,
                branch,
                worktree,
                session_id,
                pr_number,
                now,
                now,
            ),
        )
        self.connection.commit()

    def get_task(self, repo: str, issue_number: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM tasks WHERE repo=? AND issue_number=?",
            (repo, issue_number),
        ).fetchone()
        return dict(row) if row else None

    def save_delivery_checkpoint(
        self,
        *,
        repo: str,
        issue_number: int,
        task_hash: str,
        branch: str,
        worktree: str,
        context_commit: str,
        commit_sha: str,
        project_config_hash: str,
        verification_profile: str,
        verification_commands: tuple[str, ...],
        verification_result: dict[str, Any],
        structured_result: dict[str, Any],
        model: str | None,
        cli_version: str | None,
        session_id: str | None,
        phase: str = "delivery-ready",
        worker_state_key: str | None = None,
        worker_state_value: Any = None,
        task_commit_sha: str | None = None,
        integrated_base_sha: str | None = None,
        integration_refreshes: int = 0,
    ) -> None:
        now = utc_now()
        task_commit_sha = task_commit_sha or commit_sha
        integrated_base_sha = integrated_base_sha or context_commit
        with self.connection:
            existing = self.connection.execute(
                """
                SELECT branch, worktree, context_commit, commit_sha, task_commit_sha
                FROM delivery_checkpoints
                WHERE repo=? AND issue_number=? AND task_hash=?
                """,
                (repo, issue_number, task_hash),
            ).fetchone()
            identity = (branch, worktree, context_commit, commit_sha, task_commit_sha)
            if existing is not None and tuple(existing) != identity:
                raise ValueError("delivery checkpoint identity changed")

            self.connection.execute(
                """
                INSERT INTO delivery_checkpoints (
                    repo, issue_number, task_hash, branch, worktree,
                    context_commit, commit_sha, task_commit_sha,
                    integrated_base_sha, integration_refreshes, project_config_hash,
                    verification_profile, verification_commands_json,
                    verification_result_json, structured_result_json,
                    model, cli_version, session_id, phase, retryable,
                    last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          1, NULL, ?, ?)
                ON CONFLICT(repo, issue_number, task_hash) DO NOTHING
                """,
                (
                    repo,
                    issue_number,
                    task_hash,
                    branch,
                    worktree,
                    context_commit,
                    commit_sha,
                    task_commit_sha,
                    integrated_base_sha,
                    integration_refreshes,
                    project_config_hash,
                    verification_profile,
                    json.dumps(verification_commands, ensure_ascii=False, sort_keys=True),
                    json.dumps(verification_result, ensure_ascii=False, sort_keys=True),
                    json.dumps(structured_result, ensure_ascii=False, sort_keys=True),
                    model,
                    cli_version,
                    session_id,
                    phase,
                    now,
                    now,
                ),
            )
            if worker_state_key is not None:
                self.connection.execute(
                    """
                    INSERT INTO worker_state(key, value_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json=excluded.value_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        worker_state_key,
                        json.dumps(worker_state_value, sort_keys=True),
                        now,
                    ),
                )

    def get_delivery_checkpoint(
        self,
        repo: str,
        issue_number: int,
        task_hash: str,
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM delivery_checkpoints
            WHERE repo=? AND issue_number=? AND task_hash=?
            """,
            (repo, issue_number, task_hash),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["verification_commands"] = json.loads(
            item.pop("verification_commands_json")
        )
        item["verification_result"] = json.loads(
            item.pop("verification_result_json")
        )
        item["structured_result"] = json.loads(item.pop("structured_result_json"))
        item["retryable"] = bool(item["retryable"])
        return item

    def set_delivery_checkpoint_state(
        self,
        repo: str,
        issue_number: int,
        task_hash: str,
        *,
        phase: str,
        retryable: bool,
        last_error: str | None,
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE delivery_checkpoints
            SET phase=?, retryable=?, last_error=?, updated_at=?
            WHERE repo=? AND issue_number=? AND task_hash=?
            """,
            (
                phase,
                int(retryable),
                last_error[:4000] if last_error is not None else None,
                utc_now(),
                repo,
                issue_number,
                task_hash,
            ),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            raise KeyError("delivery checkpoint does not exist")

    def update_delivery_verification(
        self,
        repo: str,
        issue_number: int,
        task_hash: str,
        verification_result: dict[str, Any],
    ) -> None:
        cursor = self.connection.execute(
            """
            UPDATE delivery_checkpoints
            SET verification_result_json=?, updated_at=?
            WHERE repo=? AND issue_number=? AND task_hash=?
            """,
            (
                json.dumps(verification_result, ensure_ascii=False, sort_keys=True),
                utc_now(),
                repo,
                issue_number,
                task_hash,
            ),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            raise KeyError("delivery checkpoint does not exist")

    def active_tasks(self) -> list[dict[str, Any]]:
        terminal = ("awaiting-review", "completed", "cancelled", "needs-attention")
        placeholders = ",".join("?" for _ in terminal)
        rows = self.connection.execute(
            f"SELECT * FROM tasks WHERE state NOT IN ({placeholders}) ORDER BY updated_at",
            terminal,
        ).fetchall()
        return [dict(row) for row in rows]

    def tasks_in_states(self, states: tuple[str, ...]) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in states)
        rows = self.connection.execute(
            f"SELECT * FROM tasks WHERE state IN ({placeholders}) ORDER BY updated_at",
            states,
        ).fetchall()
        return [dict(row) for row in rows]

    def enqueue_outbox(self, kind: str, payload: dict[str, Any], idempotency_key: str) -> int:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO outbox(kind, payload_json, idempotency_key, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (kind, json.dumps(payload, sort_keys=True), idempotency_key, utc_now()),
        )
        row = self.connection.execute(
            "SELECT id FROM outbox WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        self.connection.commit()
        assert row is not None
        return int(row["id"])

    def pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM outbox
            WHERE delivered_at IS NULL AND failed_at IS NULL AND attempts < 3
            ORDER BY id LIMIT ?
            """,
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def get_outbox(self, outbox_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    def mark_outbox_delivered(self, outbox_id: int, *, remote_id: str | None = None) -> None:
        self.connection.execute(
            "UPDATE outbox SET delivered_at=?, remote_id=? WHERE id=?",
            (utc_now(), remote_id, outbox_id),
        )
        self.connection.commit()

    def reactivate_failed_outbox_and_set_worker_state(
        self,
        outbox_id: int,
        *,
        state_key: str,
        state_value: Any,
    ) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE outbox
                SET attempts=0, last_error=NULL, failed_at=NULL
                WHERE id=? AND delivered_at IS NULL AND failed_at IS NOT NULL
                """,
                (outbox_id,),
            )
            self.connection.execute(
                """
                INSERT INTO worker_state(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json=excluded.value_json,
                    updated_at=excluded.updated_at
                """,
                (state_key, json.dumps(state_value, sort_keys=True), utc_now()),
            )
        return cursor.rowcount == 1

    def record_outbox_failure(
        self,
        outbox_id: int,
        error: str,
        *,
        retryable: bool,
    ) -> None:
        row = self.connection.execute(
            "SELECT attempts FROM outbox WHERE id=?", (outbox_id,)
        ).fetchone()
        if row is None:
            return
        attempts = int(row["attempts"]) + 1
        failed_at = utc_now() if not retryable or attempts >= 3 else None
        self.connection.execute(
            "UPDATE outbox SET attempts=?, last_error=?, failed_at=? WHERE id=?",
            (attempts, error[:4000], failed_at, outbox_id),
        )
        self.connection.commit()

    def record_command(
        self,
        command_id: str,
        repo: str,
        issue_number: int,
        action: str,
        author: str,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO commands(
                command_id, repo, issue_number, action, author, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (command_id, repo, issue_number, action, author, utc_now()),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM commands WHERE command_id=?",
            (command_id,),
        ).fetchone()
        return dict(row) if row else None

    def pending_commands(self, repo: str, issue_number: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM commands
            WHERE repo=? AND issue_number=? AND executed_at IS NULL
            ORDER BY created_at
            """,
            (repo, issue_number),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_command_executed(self, command_id: str, result: str) -> None:
        self.connection.execute(
            "UPDATE commands SET executed_at=?, result=? WHERE command_id=?",
            (utc_now(), result, command_id),
        )
        self.connection.commit()

    def set_worker_state(self, key: str, value: Any) -> None:
        self.connection.execute(
            """
            INSERT INTO worker_state(key, value_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json=excluded.value_json,
                updated_at=excluded.updated_at
            """,
            (key, json.dumps(value, sort_keys=True), utc_now()),
        )
        self.connection.commit()

    def get_worker_state(self, key: str, default: Any = None) -> Any:
        row = self.connection.execute(
            "SELECT value_json FROM worker_state WHERE key=?",
            (key,),
        ).fetchone()
        return json.loads(row["value_json"]) if row else default

    def start_run(self, repo: str, issue_number: int) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(attempt), 0) + 1 AS attempt FROM runs WHERE repo=? AND issue_number=?",
            (repo, issue_number),
        ).fetchone()
        assert row is not None
        cursor = self.connection.execute(
            """
            INSERT INTO runs(repo, issue_number, attempt, started_at)
            VALUES (?, ?, ?, ?)
            """,
            (repo, issue_number, int(row["attempt"]), utc_now()),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, *, exit_code: int, result: dict[str, Any]) -> None:
        self.connection.execute(
            """
            UPDATE runs
            SET finished_at=?, exit_code=?, result_json=?
            WHERE id=?
            """,
            (utc_now(), exit_code, json.dumps(result, ensure_ascii=False, sort_keys=True), run_id),
        )
        self.connection.commit()

    def list_runs(self, repo: str, issue_number: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM runs WHERE repo=? AND issue_number=? ORDER BY attempt",
            (repo, issue_number),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json")) if item["result_json"] else None
            result.append(item)
        return result
