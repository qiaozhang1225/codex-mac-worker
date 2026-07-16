from __future__ import annotations

from pathlib import Path
import sqlite3

from codex_mac_worker.store import EventStore


def checkpoint_payload(tmp_path: Path) -> dict[str, object]:
    return {
        "repo": "owner/repo",
        "issue_number": 12,
        "task_hash": "a" * 64,
        "branch": "codex/12-layout",
        "worktree": str(tmp_path / "worktree"),
        "context_commit": "1" * 40,
        "commit_sha": "2" * 40,
        "task_commit_sha": "2" * 40,
        "integrated_base_sha": "1" * 40,
        "integration_refreshes": 0,
        "project_config_hash": "3" * 64,
        "verification_profile": "fast",
        "verification_commands": ("python -m pytest -q",),
        "verification_result": {
            "passed": True,
            "commands": [
                {
                    "command": "python -m pytest -q",
                    "exit_code": 0,
                    "output": "1 passed",
                }
            ],
        },
        "structured_result": {
            "status": "completed",
            "acceptance_results": [],
            "risks": [],
            "needs_human": [],
        },
        "model": "gpt-test",
        "cli_version": "codex-test",
        "session_id": "session-1",
    }


def test_store_uses_wal_and_persists_task_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "worker.sqlite3"
    store = EventStore(db_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=12,
        task_hash="hash-1",
        state="claimed",
        branch="codex/12-test",
        worktree="/tmp/worktree",
    )
    assert store.journal_mode == "wal"
    store.close()

    reopened = EventStore(db_path)
    task = reopened.get_task("owner/repo", 12)

    assert task is not None
    assert task["task_hash"] == "hash-1"
    assert task["state"] == "claimed"


def test_outbox_is_idempotent_and_delivery_is_persisted(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")

    first = store.enqueue_outbox("comment", {"body": "hello"}, "event:1")
    second = store.enqueue_outbox("comment", {"body": "ignored duplicate"}, "event:1")

    assert first == second
    pending = store.pending_outbox()
    assert len(pending) == 1
    assert pending[0]["payload"] == {"body": "hello"}

    store.mark_outbox_delivered(first, remote_id="99")
    assert store.pending_outbox() == []


def test_commands_execute_once(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")

    assert store.record_command("cmd-1", "owner/repo", 12, "pause", "octocat") is True
    assert store.record_command("cmd-1", "owner/repo", 12, "pause", "octocat") is False
    assert store.pending_commands("owner/repo", 12)[0]["command_id"] == "cmd-1"
    pending = store.get_command("cmd-1")
    assert pending is not None
    assert pending["executed_at"] is None

    store.mark_command_executed("cmd-1", "paused")
    assert store.pending_commands("owner/repo", 12) == []
    executed = store.get_command("cmd-1")
    assert executed is not None
    assert executed["result"] == "paused"
    assert executed["executed_at"] is not None


def test_worker_state_round_trips_json_values(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")

    store.set_worker_state("repositories", ["owner/one", "owner/two"])

    assert store.get_worker_state("repositories") == ["owner/one", "owner/two"]


def test_run_audit_records_each_attempt_result(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")

    run_id = store.start_run("owner/repo", 12)
    store.finish_run(
        run_id,
        exit_code=0,
        result={"session_id": "session-1", "status": "completed"},
    )

    runs = store.list_runs("owner/repo", 12)
    assert len(runs) == 1
    assert runs[0]["attempt"] == 1
    assert runs[0]["exit_code"] == 0
    assert runs[0]["result"]["session_id"] == "session-1"


def test_delivery_checkpoint_round_trips_and_updates_state(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")
    payload = checkpoint_payload(tmp_path)

    store.save_delivery_checkpoint(**payload)
    store.set_delivery_checkpoint_state(
        "owner/repo",
        12,
        "a" * 64,
        phase="push",
        retryable=True,
        last_error="GitError: timed out",
    )

    checkpoint = store.get_delivery_checkpoint("owner/repo", 12, "a" * 64)
    assert checkpoint is not None
    assert checkpoint["verification_commands"] == ["python -m pytest -q"]
    assert checkpoint["verification_result"]["passed"] is True
    assert checkpoint["structured_result"]["status"] == "completed"
    assert checkpoint["phase"] == "push"
    assert checkpoint["retryable"] is True
    assert checkpoint["last_error"] == "GitError: timed out"
    assert checkpoint["task_commit_sha"] == "2" * 40
    assert checkpoint["integrated_base_sha"] == "1" * 40
    assert checkpoint["integration_refreshes"] == 0


def test_delivery_checkpoint_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite3"
    store = EventStore(path)
    store.save_delivery_checkpoint(**checkpoint_payload(tmp_path))
    store.close()

    reopened = EventStore(path)
    checkpoint = reopened.get_delivery_checkpoint("owner/repo", 12, "a" * 64)

    assert checkpoint is not None
    assert checkpoint["commit_sha"] == "2" * 40
    assert checkpoint["phase"] == "delivery-ready"
    assert checkpoint["retryable"] is True


def test_delivery_checkpoint_rejects_identity_change(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")
    payload = checkpoint_payload(tmp_path)
    store.save_delivery_checkpoint(**payload)

    changed = dict(payload)
    changed["commit_sha"] = "4" * 40

    try:
        store.save_delivery_checkpoint(**changed)
    except ValueError as exc:
        assert str(exc) == "delivery checkpoint identity changed"
    else:
        raise AssertionError("expected delivery checkpoint identity rejection")


def test_delivery_checkpoint_evidence_is_immutable(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")
    payload = checkpoint_payload(tmp_path)
    store.save_delivery_checkpoint(**payload)
    changed = dict(payload)
    changed["model"] = "unexpected-model"
    changed["verification_result"] = {"passed": False, "commands": []}

    store.save_delivery_checkpoint(**changed)

    checkpoint = store.get_delivery_checkpoint("owner/repo", 12, "a" * 64)
    assert checkpoint is not None
    assert checkpoint["model"] == "gpt-test"
    assert checkpoint["verification_result"]["passed"] is True


def test_delivery_checkpoint_rejects_task_commit_identity_change(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")
    payload = checkpoint_payload(tmp_path)
    store.save_delivery_checkpoint(**payload)
    changed = dict(payload)
    changed["task_commit_sha"] = "4" * 40

    try:
        store.save_delivery_checkpoint(**changed)
    except ValueError as exc:
        assert str(exc) == "delivery checkpoint identity changed"
    else:
        raise AssertionError("expected delivery checkpoint identity rejection")


def test_old_database_migrates_delivery_integration_columns(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE delivery_checkpoints (
            repo TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            task_hash TEXT NOT NULL,
            branch TEXT NOT NULL,
            worktree TEXT NOT NULL,
            context_commit TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
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
    connection.execute(
        """
        INSERT INTO delivery_checkpoints VALUES (
            'owner/repo', 12, ?, 'codex/12-task', '/tmp/worktree', ?, ?, ?,
            'fast', '[]', '{"passed": true}', '{"status": "completed"}',
            NULL, NULL, NULL, 'complete', 0, NULL, 'now', 'now'
        )
        """,
        ("a" * 64, "1" * 40, "2" * 40, "3" * 64),
    )
    connection.commit()
    connection.close()

    store = EventStore(path)
    checkpoint = store.get_delivery_checkpoint("owner/repo", 12, "a" * 64)

    assert checkpoint is not None
    assert checkpoint["task_commit_sha"] == "2" * 40
    assert checkpoint["integrated_base_sha"] == "1" * 40
    assert checkpoint["integration_refreshes"] == 0


def test_auto_merge_operation_is_idempotent_and_rejects_identity_drift(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")
    payload = {
        "repo": "owner/repo",
        "issue_number": 12,
        "pr_number": 44,
        "task_hash": "a" * 64,
        "expected_head": "c" * 40,
    }

    first = store.begin_auto_merge(**payload)
    second = store.begin_auto_merge(**payload)

    assert first["state"] == "recorded"
    assert second["state"] == "recorded"
    try:
        store.begin_auto_merge(**(payload | {"expected_head": "d" * 40}))
    except ValueError as exc:
        assert "identity changed" in str(exc)
    else:
        raise AssertionError("expected auto-merge identity drift rejection")


def test_delivery_integration_update_preserves_task_identity(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")
    payload = checkpoint_payload(tmp_path)
    store.save_delivery_checkpoint(**payload)

    store.update_delivery_integration(
        "owner/repo",
        12,
        "a" * 64,
        expected_task_commit="2" * 40,
        previous_head="2" * 40,
        delivery_head="4" * 40,
        integrated_base="5" * 40,
        integration_refreshes=1,
        verification_result={"passed": True, "commands": [{"command": "pytest"}]},
        verification_commands=("pytest",),
        project_config_hash="6" * 64,
    )

    checkpoint = store.get_delivery_checkpoint("owner/repo", 12, "a" * 64)
    assert checkpoint is not None
    assert checkpoint["task_commit_sha"] == "2" * 40
    assert checkpoint["commit_sha"] == "4" * 40
    assert checkpoint["integrated_base_sha"] == "5" * 40
    assert checkpoint["integration_refreshes"] == 1


def test_legacy_checkpoint_and_reconstruction_marker_are_saved_together(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")
    payload = checkpoint_payload(tmp_path)

    store.save_delivery_checkpoint(
        **payload,
        phase="legacy-reconstructed",
        worker_state_key="legacy-delivery-recovery:owner/repo#12:hash",
        worker_state_value="reconstructed",
    )

    checkpoint = store.get_delivery_checkpoint("owner/repo", 12, "a" * 64)
    assert checkpoint is not None
    assert checkpoint["phase"] == "legacy-reconstructed"
    assert checkpoint["retryable"] is True
    assert store.get_worker_state(
        "legacy-delivery-recovery:owner/repo#12:hash"
    ) == "reconstructed"
