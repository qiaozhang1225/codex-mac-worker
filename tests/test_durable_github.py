from __future__ import annotations

from pathlib import Path

import pytest

from codex_mac_worker.durable_github import DurableGitHub
from codex_mac_worker.store import EventStore


class FlakyGitHub:
    def __init__(self) -> None:
        self.fail = True
        self.comments: list[str] = []

    def add_comment(self, repo: str, issue_number: int, body: str) -> dict:
        if self.fail:
            raise RuntimeError("offline")
        self.comments.append(body)
        return {"id": 99}

    def list_comments(self, repo: str, issue_number: int) -> list[dict]:
        return [{"id": 99, "body": body} for body in self.comments]


def test_failed_write_remains_in_outbox_and_flushes_after_recovery(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")
    remote = FlakyGitHub()
    durable = DurableGitHub(remote, store)

    with pytest.raises(RuntimeError, match="offline"):
        durable.add_comment("owner/repo", 12, "status")

    assert len(store.pending_outbox()) == 1
    remote.fail = False
    durable.flush()
    assert store.pending_outbox() == []
    assert remote.comments == ["status"]


def test_comment_delivery_is_reconciled_without_duplicate(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")
    remote = FlakyGitHub()
    remote.fail = False
    remote.comments.append("status")
    durable = DurableGitHub(remote, store)
    store.enqueue_outbox(
        "github",
        {"operation": "add_comment", "repo": "owner/repo", "issue_number": 12, "body": "status"},
        "existing",
    )

    durable.flush()

    assert remote.comments == ["status"]
    assert store.pending_outbox() == []


def test_outbox_stops_after_initial_attempt_and_two_retries(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")
    remote = FlakyGitHub()
    durable = DurableGitHub(remote, store)

    with pytest.raises(RuntimeError, match="offline"):
        durable.add_comment("owner/repo", 12, "bounded retry")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="offline"):
            durable.flush()

    assert store.pending_outbox() == []
    row = store.connection.execute(
        "SELECT attempts, failed_at FROM outbox WHERE delivered_at IS NULL"
    ).fetchone()
    assert row is not None
    assert row["attempts"] == 3
    assert row["failed_at"] is not None


def test_durable_github_persists_pull_request_body_updates(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")

    class Remote:
        def update_pull_request(self, repo: str, pr_number: int, *, body: str) -> dict:
            return {"id": pr_number, "number": pr_number, "body": body}

    github = DurableGitHub(Remote(), store)

    first = github.update_pull_request("owner/repo", 44, body="new evidence")
    second = github.update_pull_request("owner/repo", 44, body="new evidence")

    assert first["body"] == "new evidence"
    assert second == {"id": 44}
    assert store.pending_outbox() == []
