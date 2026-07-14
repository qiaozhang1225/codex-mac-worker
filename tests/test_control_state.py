from __future__ import annotations

from pathlib import Path

from codex_mac_worker.control_state import ControlState, operation_id


def test_operation_ledger_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "codexctl.sqlite3"
    key = operation_id("task-merge", "owner/repo#44", "a" * 40)
    state = ControlState(path)
    assert state.journal_mode == "wal"
    assert state.begin(key, "task-merge", "owner/repo#44", "a" * 40) is True
    assert state.begin(key, "task-merge", "owner/repo#44", "a" * 40) is False
    state.record_context(key, {"actor_login": "owner", "fingerprint": "f" * 64})
    assert state.get(key)["result"]["actor_login"] == "owner"
    state.complete(key, {"merged": True, "sha": "b" * 40})
    state.close()

    reopened = ControlState(path)
    record = reopened.get(key)
    assert record is not None
    assert record["state"] == "completed"
    assert record["result"] == {"merged": True, "sha": "b" * 40}
    reopened.close()


def test_operation_id_binds_action_target_and_expected_head() -> None:
    baseline = operation_id("task-merge", "owner/repo#44", "a" * 40)

    assert len(baseline) == 64
    assert baseline != operation_id("repo-finalize", "owner/repo#44", "a" * 40)
    assert baseline != operation_id("task-merge", "owner/repo#45", "a" * 40)
    assert baseline != operation_id("task-merge", "owner/repo#44", "b" * 40)
