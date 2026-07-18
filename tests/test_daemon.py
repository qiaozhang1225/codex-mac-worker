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
        self.issue_updates: list[dict] = []

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
        update = {"issue_number": issue_number, "labels": labels, "state": state}
        self.issue_updates.append(update)
        return update


class FakeService:
    def __init__(self) -> None:
        self.processed: list[int] = []
        self.resumed: list[str | None] = []
        self.delivery_retried: list[int] = []
        self.validated: list[str] = []
        self.stopped = False
        self.auto_merge_calls: list[int] = []
        self.auto_merge_result = "completed"

    def stop(self) -> None:
        self.stopped = True

    def validate_repository_authority(self, repository: RepositoryConfig) -> None:
        self.validated.append(repository.name)

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

    def retry_delivery(
        self,
        repository: RepositoryConfig,
        issue: dict,
    ) -> str:
        self.delivery_retried.append(issue["number"])
        return "awaiting-review"

    def auto_merge_delivery(
        self,
        repository: RepositoryConfig,
        issue: dict,
        task: dict,
    ) -> str:
        self.auto_merge_calls.append(issue["number"])
        return self.auto_merge_result


def config(tmp_path: Path) -> WorkerConfig:
    return WorkerConfig(
        "mac-mini", 60, 120, tmp_path / "state.sqlite3", tmp_path / "cache",
        tmp_path / "worktrees", tmp_path / "outputs", Path("/tmp/codex"), "123", "456",
        tmp_path / "app.pem", ("owner",), (RepositoryConfig("owner/repo", "url"),),
    )


def automatic_config(tmp_path: Path) -> WorkerConfig:
    settings = config(tmp_path)
    return WorkerConfig(
        settings.worker_id,
        settings.poll_seconds,
        settings.heartbeat_seconds,
        settings.database_path,
        settings.cache_root,
        settings.worktree_root,
        settings.output_root,
        settings.codex_path,
        settings.github_app_id,
        settings.github_installation_id,
        settings.github_private_key_path,
        settings.authorized_users,
        settings.repositories,
        merge_mode="automatic",
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
    assert service.validated == ["owner/repo"]


def test_daemon_isolates_invalid_static_repository_and_processes_valid_one(
    tmp_path: Path,
) -> None:
    from codex_mac_worker.config import ConfigError

    settings = config(tmp_path)
    settings = WorkerConfig(
        settings.worker_id,
        settings.poll_seconds,
        settings.heartbeat_seconds,
        settings.database_path,
        settings.cache_root,
        settings.worktree_root,
        settings.output_root,
        settings.codex_path,
        settings.github_app_id,
        settings.github_installation_id,
        settings.github_private_key_path,
        settings.authorized_users,
        (
            RepositoryConfig("owner/legacy-v1", "legacy-url"),
            RepositoryConfig("owner/ready", "ready-url"),
        ),
    )

    class PartiallyInvalidService(FakeService):
        def validate_repository_authority(
            self, repository: RepositoryConfig
        ) -> None:
            super().validate_repository_authority(repository)
            if repository.name == "owner/legacy-v1":
                raise ConfigError("project schema_version 1 must be migrated to 2")

    service = PartiallyInvalidService()
    store = EventStore(settings.database_path)
    daemon = WorkerDaemon(
        settings,
        FakeGitHub([{"number": 21, "created_at": "2026-01-01T00:00:00Z"}]),
        store,
        service,
    )

    assert daemon.run_once() is True
    assert service.validated == ["owner/legacy-v1", "owner/ready"]
    assert service.processed == [21]
    assert store.get_worker_state("repository_eligibility:owner/legacy-v1") == {
        "eligible": False,
        "error": "ConfigError: project schema_version 1 must be migrated to 2",
    }


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


def test_daemon_routes_retry_to_delivery_without_process_issue(tmp_path: Path) -> None:
    settings = config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        state="needs-attention",
        branch="codex/9-active",
        worktree="/tmp/worktree",
    )
    service = FakeService()

    class RetryGitHub(FakeGitHub):
        def list_comments(self, repo: str, issue_number: int) -> list[dict]:
            return [
                {
                    "body": render_command_comment(
                        action="retry",
                        issue_number=9,
                        requirements=(),
                        command_id="cmd-retry",
                    ),
                    "user": {"login": "owner"},
                }
            ]

    daemon = WorkerDaemon(settings, RetryGitHub([]), store, service)

    assert daemon.process_control_commands() is True
    assert service.delivery_retried == [9]
    assert service.processed == []
    command = store.get_command("cmd-retry")
    assert command is not None and command["result"] == "awaiting-review"


def test_daemon_retries_pre_execution_failure_through_process_issue(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        state="needs-attention",
        branch="codex/9-active",
    )
    store.record_command("cmd-pre-execution-retry", "owner/repo", 9, "retry", "owner")
    service = FakeService()
    daemon = WorkerDaemon(settings, FakeGitHub([]), store, service)

    assert daemon.process_control_commands() is True

    assert service.processed == [9]
    assert service.resumed == [None]
    assert service.delivery_retried == []
    command = store.get_command("cmd-pre-execution-retry")
    assert command is not None
    assert command["result"] == "pre-execution-retry"


@pytest.mark.parametrize(
    ("checkpoint_phase", "checkpoint_error", "prior_command_result"),
    [
        ("complete", None, None),
        (
            "validation",
            "PolicyError: delivery checkpoint is not retryable",
            "awaiting-review",
        ),
    ],
)
def test_retry_rearms_completed_automatic_delivery_for_merge_loop(
    tmp_path: Path,
    checkpoint_phase: str,
    checkpoint_error: str | None,
    prior_command_result: str | None,
) -> None:
    settings = automatic_config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        state="needs-attention",
        branch="codex/9-active",
        worktree="/tmp/worktree",
        session_id="codex-session-must-not-run",
        pr_number=44,
    )
    store.save_delivery_checkpoint(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        branch="codex/9-active",
        worktree="/tmp/worktree",
        context_commit="1" * 40,
        commit_sha="2" * 40,
        project_config_hash="3" * 64,
        verification_profile="fast",
        verification_commands=("pytest -q",),
        verification_result={"passed": True, "commands": []},
        structured_result={"status": "completed"},
        model="gpt-test",
        cli_version="codex-test",
        session_id="codex-session-must-not-run",
    )
    store.set_delivery_checkpoint_state(
        "owner/repo",
        9,
        "hash",
        phase=checkpoint_phase,
        retryable=False,
        last_error=checkpoint_error,
    )
    if prior_command_result is not None:
        store.record_command(
            "cmd-prior-delivery", "owner/repo", 9, "retry", "owner"
        )
        store.mark_command_executed(
            "cmd-prior-delivery", prior_command_result
        )
    store.record_command("cmd-auto-retry", "owner/repo", 9, "retry", "owner")

    class LabelGitHub(FakeGitHub):
        def __init__(self) -> None:
            super().__init__([])
            self.label_updates: list[list[str]] = []

        def get_issue(self, repo: str, issue_number: int) -> dict:
            return {
                "number": issue_number,
                "labels": [{"name": "codex:needs-attention"}],
            }

        def set_labels(
            self,
            repo: str,
            issue_number: int,
            labels: list[str],
        ) -> dict:
            self.label_updates.append(labels)
            return {"labels": labels}

    github = LabelGitHub()
    service = FakeService()
    daemon = WorkerDaemon(settings, github, store, service)

    assert daemon.process_control_commands() is True

    task = store.get_task("owner/repo", 9)
    command = store.get_command("cmd-auto-retry")
    assert task is not None and task["state"] == "merging"
    assert task["pr_number"] == 44
    assert task["session_id"] == "codex-session-must-not-run"
    assert command is not None and command["result"] == "merging"
    assert service.delivery_retried == []
    assert service.processed == []
    assert github.label_updates[-1] == ["codex:merging"]

    assert daemon.process_review_tasks() is True
    assert service.auto_merge_calls == [9]
    assert store.get_task("owner/repo", 9)["state"] == "completed"


@pytest.mark.parametrize(
    ("task_state", "checkpoint_phase", "checkpoint_error"),
    [
        (
            "needs-attention",
            "validation",
            "PolicyError: delivery checkpoint is not retryable",
        ),
        ("paused", "complete", None),
    ],
)
def test_retry_does_not_adopt_checkpoint_outside_eligible_state(
    tmp_path: Path,
    task_state: str,
    checkpoint_phase: str,
    checkpoint_error: str | None,
) -> None:
    settings = automatic_config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        state=task_state,
        branch="codex/9-active",
        worktree="/tmp/worktree",
        pr_number=44,
    )
    store.save_delivery_checkpoint(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        branch="codex/9-active",
        worktree="/tmp/worktree",
        context_commit="1" * 40,
        commit_sha="2" * 40,
        project_config_hash="3" * 64,
        verification_profile="fast",
        verification_commands=("pytest -q",),
        verification_result={"passed": True, "commands": []},
        structured_result={"status": "completed"},
        model="gpt-test",
        cli_version="codex-test",
        session_id=None,
    )
    store.set_delivery_checkpoint_state(
        "owner/repo",
        9,
        "hash",
        phase=checkpoint_phase,
        retryable=False,
        last_error=checkpoint_error,
    )
    store.record_command("cmd-unsafe-retry", "owner/repo", 9, "retry", "owner")
    service = FakeService()
    daemon = WorkerDaemon(settings, FakeGitHub([]), store, service)

    assert daemon.process_control_commands() is True

    assert service.delivery_retried == [9]
    assert store.get_task("owner/repo", 9)["state"] == "retrying"
    command = store.get_command("cmd-unsafe-retry")
    assert command is not None and command["result"] == "awaiting-review"


def test_pending_retry_command_resumes_after_crash_without_comment(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        state="needs-attention",
        branch="codex/9-active",
        worktree="/tmp/worktree",
    )
    store.record_command("cmd-retry", "owner/repo", 9, "retry", "owner")
    service = FakeService()
    daemon = WorkerDaemon(settings, FakeGitHub([]), store, service)

    assert daemon.process_control_commands() is True
    assert service.delivery_retried == [9]
    command = store.get_command("cmd-retry")
    assert command is not None and command["executed_at"] is not None


def test_pending_retry_command_is_reconciled_after_delivery_completed(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        state="awaiting-review",
        branch="codex/9-active",
        worktree="/tmp/worktree",
        pr_number=44,
    )
    store.record_command("cmd-retry", "owner/repo", 9, "retry", "owner")
    service = FakeService()
    daemon = WorkerDaemon(settings, FakeGitHub([]), store, service)

    assert daemon.run_once() is True

    command = store.get_command("cmd-retry")
    assert command is not None
    assert command["result"] == "awaiting-review"
    assert command["executed_at"] is not None
    assert service.delivery_retried == []


def test_resume_paused_delivery_verification_never_invokes_codex(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        state="paused",
        branch="codex/9-active",
        worktree="/tmp/worktree",
        session_id="codex-session-must-not-resume",
    )
    store.save_delivery_checkpoint(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        branch="codex/9-active",
        worktree="/tmp/worktree",
        context_commit="1" * 40,
        commit_sha="2" * 40,
        project_config_hash="3" * 64,
        verification_profile="fast",
        verification_commands=("pytest -q",),
        verification_result={"passed": True, "commands": []},
        structured_result={"status": "completed"},
        model="gpt-test",
        cli_version="codex-test",
        session_id="codex-session-must-not-resume",
    )
    store.set_delivery_checkpoint_state(
        "owner/repo",
        9,
        "hash",
        phase="paused-verification",
        retryable=True,
        last_error=None,
    )
    store.record_command("cmd-resume", "owner/repo", 9, "resume", "owner")
    service = FakeService()
    daemon = WorkerDaemon(settings, FakeGitHub([]), store, service)

    assert daemon.process_control_commands() is True

    assert service.delivery_retried == [9]
    assert service.processed == []
    assert store.get_command("cmd-resume")["result"] == "awaiting-review"


def test_executed_historical_retry_command_is_never_replayed(tmp_path: Path) -> None:
    settings = config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        state="needs-attention",
        branch="codex/9-active",
        worktree="/tmp/worktree",
    )
    command_id = "503e56c5-64a7-474b-8364-299c6f929272"
    store.record_command(command_id, "owner/repo", 9, "retry", "owner")
    store.mark_command_executed(command_id, "not-retryable")
    service = FakeService()
    daemon = WorkerDaemon(settings, FakeGitHub([]), store, service)

    assert daemon.process_control_commands() is False
    assert service.delivery_retried == []


def test_retry_command_remains_pending_when_service_crashes(tmp_path: Path) -> None:
    settings = config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        state="needs-attention",
        branch="codex/9-active",
        worktree="/tmp/worktree",
    )

    class RetryGitHub(FakeGitHub):
        def list_comments(self, repo: str, issue_number: int) -> list[dict]:
            return [
                {
                    "body": render_command_comment(
                        action="retry",
                        issue_number=9,
                        requirements=(),
                        command_id="cmd-crash",
                    ),
                    "user": {"login": "owner"},
                }
            ]

    class CrashService(FakeService):
        def retry_delivery(
            self,
            repository: RepositoryConfig,
            issue: dict,
        ) -> str:
            raise SystemExit("simulated crash")

    with pytest.raises(SystemExit, match="simulated crash"):
        WorkerDaemon(settings, RetryGitHub([]), store, CrashService()).process_control_commands()

    command = store.get_command("cmd-crash")
    assert command is not None and command["executed_at"] is None
    recovery_service = FakeService()
    recovery_daemon = WorkerDaemon(settings, FakeGitHub([]), store, recovery_service)
    assert recovery_daemon.recover_active_tasks() is True
    assert recovery_daemon.process_control_commands() is True
    assert recovery_service.delivery_retried == [9]


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


def test_daemon_auto_merges_existing_awaiting_review_task(tmp_path: Path) -> None:
    settings = automatic_config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        state="awaiting-review",
        branch="codex/9-active",
        worktree="/tmp/worktree",
        pr_number=44,
    )
    github = FakeGitHub([])
    service = FakeService()
    daemon = WorkerDaemon(settings, github, store, service)

    assert daemon.process_review_tasks() is True

    assert service.auto_merge_calls == [9]
    assert store.get_task("owner/repo", 9)["state"] == "completed"
    assert github.issue_updates[-1] == {
        "issue_number": 9,
        "labels": ["codex:completed"],
        "state": "closed",
    }


def test_daemon_automatic_mode_reconciles_already_merged_pr_through_service(
    tmp_path: Path,
) -> None:
    settings = automatic_config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        state="merging",
        branch="codex/9-active",
        worktree="/tmp/worktree",
        pr_number=44,
    )

    class MergedGitHub(FakeGitHub):
        def get_pull_request(self, repo: str, pr_number: int) -> dict:
            return {"number": pr_number, "merged_at": "2026-07-17T00:00:00Z"}

    github = MergedGitHub([])
    service = FakeService()
    service.auto_merge_result = "needs-attention"
    daemon = WorkerDaemon(settings, github, store, service)

    assert daemon.process_review_tasks() is True
    assert service.auto_merge_calls == [9]
    assert store.get_task("owner/repo", 9)["state"] == "needs-attention"
    assert github.issue_updates == []


def test_daemon_manual_mode_leaves_delivery_for_human_review(tmp_path: Path) -> None:
    settings = config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        state="awaiting-review",
        branch="codex/9-active",
        worktree="/tmp/worktree",
        pr_number=44,
    )
    service = FakeService()
    daemon = WorkerDaemon(settings, FakeGitHub([]), store, service)

    assert daemon.process_review_tasks() is False
    assert service.auto_merge_calls == []
    assert store.get_task("owner/repo", 9)["state"] == "awaiting-review"


@pytest.mark.parametrize(
    ("service_result", "expected_state"),
    [("merging", "merging"), ("needs-attention", "needs-attention")],
)
def test_daemon_reconciles_auto_merge_outcomes(
    tmp_path: Path, service_result: str, expected_state: str
) -> None:
    settings = automatic_config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        state="merging",
        branch="codex/9-active",
        worktree="/tmp/worktree",
        pr_number=44,
    )
    service = FakeService()
    service.auto_merge_result = service_result
    daemon = WorkerDaemon(settings, FakeGitHub([]), store, service)

    assert daemon.process_review_tasks() is True
    assert store.get_task("owner/repo", 9)["state"] == expected_state
    assert service.processed == []


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
