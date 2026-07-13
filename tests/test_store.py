from __future__ import annotations

from pathlib import Path

from codex_mac_worker.store import EventStore


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

    store.mark_command_executed("cmd-1", "paused")
    assert store.pending_commands("owner/repo", 12) == []


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
