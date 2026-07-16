from __future__ import annotations

from pathlib import Path

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


def test_delivery_checkpoint_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite3"
    store = EventStore(path)
    store.save_delivery_checkpoint(**checkpoint_payload(tmp_path))
    store.close()

    reopened = EventStore(path)
    checkpoint = reopened.get_delivery_checkpoint("owner/repo", 12, "a" * 64)

    assert checkpoint is not None
    assert checkpoint["commit_sha"] == "2" * 40


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
