from __future__ import annotations

from pathlib import Path

import pytest

from codex_mac_worker.config import RepositoryConfig, WorkerConfig
from codex_mac_worker.daemon import AlreadyRunning, SingleInstanceLock, WorkerDaemon
from codex_mac_worker.protocol import render_command_comment
from codex_mac_worker.store import EventStore


class FakeGitHub:
    def __init__(self, issues: list[dict]) -> None:
        self.issues = issues

    def list_queued_issues(self, repo: str) -> list[dict]:
        return self.issues

    def get_issue(self, repo: str, issue_number: int) -> dict:
        return {"number": issue_number, "labels": [{"name": "codex:claimed"}]}

    def list_comments(self, repo: str, issue_number: int) -> list[dict]:
        return []

    def collaborator_permission(self, repo: str, username: str) -> str:
        return "write"

    def set_labels(self, repo: str, issue_number: int, labels: list[str]) -> dict:
        return {"labels": labels}

    def flush(self) -> None:
        return None

    def get_pull_request(self, repo: str, pr_number: int) -> dict:
        return {"number": pr_number, "merged_at": None}

    def update_issue(
        self,
        repo: str,
        issue_number: int,
        *,
        labels: list[str] | None = None,
        state: str | None = None,
    ) -> dict:
        return {"labels": labels, "state": state}


class FakeService:
    def __init__(self) -> None:
        self.processed: list[int] = []
        self.resumed: list[str | None] = []
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    def process_issue(
        self,
        repository: RepositoryConfig,
        issue: dict,
        resume_session_id: str | None = None,
    ) -> None:
        self.processed.append(issue["number"])
        self.resumed.append(resume_session_id)

    def revise_issue(
        self,
        repository: RepositoryConfig,
        issue: dict,
        task: dict,
        requirements: tuple[str, ...],
    ) -> None:
        self.processed.append(issue["number"])


def config(tmp_path: Path) -> WorkerConfig:
    return WorkerConfig(
        "mac-mini", 60, 120, tmp_path / "state.sqlite3", tmp_path / "cache",
        tmp_path / "worktrees", tmp_path / "outputs", Path("/tmp/codex"), "123", "456",
        tmp_path / "app.pem", ("owner",), (RepositoryConfig("owner/repo", "url"),),
    )


def test_daemon_processes_only_oldest_queued_issue(tmp_path: Path) -> None:
    issues = [
        {"number": 20, "created_at": "2026-01-02T00:00:00Z"},
        {"number": 10, "created_at": "2026-01-01T00:00:00Z"},
    ]
    service = FakeService()
    daemon = WorkerDaemon(config(tmp_path), FakeGitHub(issues), EventStore(tmp_path / "state.sqlite3"), service)

    assert daemon.run_once() is True
    assert service.processed == [10]


def test_daemon_does_not_claim_new_issue_with_active_local_task(tmp_path: Path) -> None:
    settings = config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo", issue_number=9, task_hash="hash", state="running",
        branch="codex/9-active", worktree="/tmp/worktree",
    )
    service = FakeService()
    daemon = WorkerDaemon(
        settings,
        FakeGitHub([{"number": 10, "created_at": "2026-01-01T00:00:00Z"}]),
        store,
        service,
    )

    assert daemon.run_once() is False
    assert service.processed == []


def test_daemon_reconciles_crashed_active_task_to_attention_without_reexecution(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo", issue_number=9, task_hash="hash", state="running",
        branch="codex/9-active", worktree="/tmp/worktree",
    )
    service = FakeService()
    github = FakeGitHub([])
    daemon = WorkerDaemon(settings, github, store, service)

    assert daemon.recover_active_tasks() is True
    assert daemon.recover_active_tasks() is False
    assert service.processed == []
    assert store.get_task("owner/repo", 9)["state"] == "needs-attention"


def test_daemon_executes_authorized_resume_for_paused_task(tmp_path: Path) -> None:
    settings = config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo", issue_number=9, task_hash="hash", state="paused",
        branch="codex/9-active", worktree="/tmp/worktree", session_id="session-paused",
    )
    service = FakeService()

    class ResumeGitHub(FakeGitHub):
        def list_comments(self, repo: str, issue_number: int) -> list[dict]:
            return [{
                "body": render_command_comment(
                    action="resume", issue_number=9, requirements=(), command_id="cmd-resume"
                ),
                "user": {"login": "owner"},
            }]

    daemon = WorkerDaemon(settings, ResumeGitHub([]), store, service)

    assert daemon.process_control_commands() is True
    assert service.processed == [9]
    assert service.resumed == ["session-paused"]


def test_daemon_executes_authorized_revision_on_existing_pr(tmp_path: Path) -> None:
    settings = config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo", issue_number=9, task_hash="hash", state="awaiting-review",
        branch="codex/9-active", worktree="/tmp/worktree", pr_number=44,
    )
    service = FakeService()

    class RevisionGitHub(FakeGitHub):
        def list_comments(self, repo: str, issue_number: int) -> list[dict]:
            return [{
                "body": render_command_comment(
                    action="revise",
                    issue_number=9,
                    requirements=("Rename the public heading",),
                    command_id="cmd-revise",
                ),
                "user": {"login": "owner"},
            }]

    daemon = WorkerDaemon(settings, RevisionGitHub([]), store, service)

    assert daemon.process_review_tasks() is True
    assert service.processed == [9]
    assert store.pending_commands("owner/repo", 9) == []


def test_daemon_closes_issue_only_after_pr_is_merged(tmp_path: Path) -> None:
    settings = config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo", issue_number=9, task_hash="hash", state="awaiting-review",
        branch="codex/9-active", worktree="/tmp/worktree", pr_number=44,
    )
    updates: list[dict] = []

    class MergedGitHub(FakeGitHub):
        def get_pull_request(self, repo: str, pr_number: int) -> dict:
            return {"number": pr_number, "merged_at": "2026-07-13T01:00:00Z"}

        def update_issue(
            self,
            repo: str,
            issue_number: int,
            *,
            labels: list[str] | None = None,
            state: str | None = None,
        ) -> dict:
            updates.append({"labels": labels, "state": state})
            return updates[-1]

    daemon = WorkerDaemon(settings, MergedGitHub([]), store, FakeService())

    assert daemon.process_review_tasks() is True
    assert store.get_task("owner/repo", 9)["state"] == "completed"
    assert updates == [{"labels": ["codex:completed"], "state": "closed"}]


def test_single_instance_lock_rejects_second_worker(tmp_path: Path) -> None:
    lock_path = tmp_path / "worker.lock"

    with SingleInstanceLock(lock_path):
        with pytest.raises(AlreadyRunning):
            with SingleInstanceLock(lock_path):
                raise AssertionError("second worker must not enter")


def test_daemon_stop_propagates_to_active_service(tmp_path: Path) -> None:
    service = FakeService()
    daemon = WorkerDaemon(config(tmp_path), FakeGitHub([]), EventStore(tmp_path / "state.sqlite3"), service)

    daemon.stop()

    assert service.stopped is True
