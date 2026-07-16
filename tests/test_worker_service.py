from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

import codex_mac_worker.worker as worker_module
from codex_mac_worker.config import RepositoryConfig, WorkerConfig
from codex_mac_worker.durable_github import DurableGitHub
from codex_mac_worker.github import GitHubError
from codex_mac_worker.gitops import GitError, GitOperations
from codex_mac_worker.protocol import parse_delivery_block, parse_task_body
from codex_mac_worker.runner import RunnerResult, RunnerTimeout
from codex_mac_worker.store import EventStore
from codex_mac_worker.verification import CommandResult, VerificationResult
from codex_mac_worker.worker import WorkerService
from codex_mac_worker.automatic_merge import AutoMergeBlocked, AutomaticMergeResult

from .test_protocol import VALID_SHA, task_body


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def project_config_text(*, worker_github_app_id: int = 123) -> str:
    return f"""
schema_version = 2
default_base_branch = "main"
worker_github_app_id = {worker_github_app_id}
allowed_risk_levels = ["low", "medium"]
protected_paths = [".codex-worker", ".github/workflows", ".env"]
max_changed_files = 10
max_diff_lines = 100
codex_attempt_timeout_minutes = 1
task_hard_timeout_minutes = 2
max_automatic_attempts = 2
[verification.fast]
commands = ["{sys.executable} -c 'print(123)'"]
""".strip() + "\n"


def make_project_remote(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    (source / ".codex-worker").mkdir()
    (source / ".codex-worker" / "project.toml").write_text(
        project_config_text(), encoding="utf-8"
    )
    (source / "docs").mkdir()
    (source / "docs" / "spec.md").write_text("spec\n", encoding="utf-8")
    (source / "src").mkdir()
    (source / "src" / "base.txt").write_text("base\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "baseline")
    sha = git(source, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(source), str(remote))
    return remote, sha


class FakeRunner:
    def run(self, worktree: Path, prompt: str, output_schema: Path, **kwargs: object) -> RunnerResult:
        (worktree / "src" / "result.txt").write_text("implemented\n", encoding="utf-8")
        return RunnerResult(
            0,
            "session-1",
            (),
            '{"status":"completed","summary":"done","changed_files":["src/result.txt"],'
            '"acceptance_results":[{"criterion":"Unit tests pass","status":"met",'
            '"evidence":"fast verification"}],"risks":[],"needs_human":[]}',
            "",
            model="gpt-test",
            cli_version="codex-test",
        )


class FakeGitHub:
    def __init__(self, issue: dict) -> None:
        self.issue = issue
        self.current_project_config = project_config_text()
        self.labels: list[list[str]] = []
        self.comments: list[str] = []
        self.prs: list[dict] = []
        self.updated_prs: list[dict] = []

    def get_issue(self, repo: str, issue_number: int) -> dict:
        return self.issue

    def set_labels(self, repo: str, issue_number: int, labels: list[str]) -> dict:
        self.labels.append(labels)
        self.issue["labels"] = [{"name": label} for label in labels]
        return {"labels": self.issue["labels"]}

    def add_comment(self, repo: str, issue_number: int, body: str) -> dict:
        self.comments.append(body)
        return {"id": 90 + len(self.comments)}

    def update_comment(self, repo: str, comment_id: int, body: str) -> dict:
        self.comments.append(body)
        return {"id": comment_id}

    def list_comments(self, repo: str, issue_number: int) -> list[dict]:
        return []

    def collaborator_permission(self, repo: str, username: str) -> str:
        return "write"

    def get_repository(self, repo: str) -> dict:
        return {"default_branch": "main"}

    def get_commit(self, repo: str, ref: str) -> dict:
        return {"sha": parse_task_body(self.issue["body"]).context_commit}

    def get_repository_file(self, repo: str, path: str, *, ref: str) -> str:
        assert path == ".codex-worker/project.toml"
        return self.current_project_config

    def create_draft_pr(self, repo: str, head: str, base: str, title: str, body: str) -> dict:
        payload = {"number": 44, "html_url": "https://example/pr/44", "head": head, "body": body}
        self.prs.append(payload)
        return payload

    def update_pull_request(self, repo: str, pr_number: int, *, body: str) -> dict:
        payload = {"number": pr_number, "body": body}
        self.updated_prs.append(payload)
        self.prs[0]["body"] = body
        return payload


def make_worker_fixture(
    tmp_path: Path,
) -> tuple[
    WorkerService,
    WorkerConfig,
    FakeGitHub,
    EventStore,
    GitOperations,
    dict,
]:
    remote, sha = make_project_remote(tmp_path)
    issue = {
        "number": 12,
        "title": "Bounded task",
        "body": task_body(sha=sha),
        "labels": [{"name": "codex:queued"}],
        "user": {"login": "owner"},
    }
    github = FakeGitHub(issue)
    config = WorkerConfig(
        "mac-mini",
        60,
        120,
        tmp_path / "state.sqlite3",
        tmp_path / "cache",
        tmp_path / "worktrees",
        tmp_path / "outputs",
        Path("/tmp/codex"),
        "123",
        "456",
        tmp_path / "app.pem",
        ("owner",),
        (RepositoryConfig("owner/repo", str(remote)),),
    )
    store = EventStore(config.database_path)
    operations = GitOperations(
        cache_root=config.cache_root,
        worktree_root=config.worktree_root,
    )
    service = WorkerService(
        config=config,
        github=github,
        token_provider=lambda: "token",
        store=store,
        git=operations,
        runner=FakeRunner(),
    )
    return service, config, github, store, operations, issue


def test_worker_processes_bounded_task_into_draft_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, sha = make_project_remote(tmp_path)
    body = task_body(sha=sha)
    issue = {
        "number": 12,
        "title": "Bounded task",
        "body": body,
        "labels": [{"name": "codex:queued"}, {"name": "priority:p1"}],
        "user": {"login": "owner"},
    }
    github = FakeGitHub(issue)
    config = WorkerConfig(
        worker_id="mac-mini",
        poll_seconds=60,
        heartbeat_seconds=120,
        database_path=tmp_path / "state.sqlite3",
        cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
        output_root=tmp_path / "outputs",
        codex_path=Path("/tmp/codex"),
        github_app_id="123",
        github_installation_id="456",
        github_private_key_path=tmp_path / "app.pem",
        authorized_users=("owner",),
        repositories=(RepositoryConfig("owner/repo", str(remote)),),
    )
    store = EventStore(config.database_path)
    operations = GitOperations(cache_root=config.cache_root, worktree_root=config.worktree_root)
    service = WorkerService(
        config=config,
        github=github,
        token_provider=lambda: "token",
        store=store,
        git=operations,
        runner=FakeRunner(),
    )
    preparation_profiles: list[object] = []

    def capture_preparation(*args: object, **kwargs: object) -> VerificationResult:
        preparation_profiles.append(kwargs.get("permission_profile"))
        return VerificationResult(True, ())

    monkeypatch.setattr(worker_module, "run_commands", capture_preparation)

    service.process_issue(config.repositories[0], issue)

    task = store.get_task("owner/repo", 12)
    assert task is not None
    assert task["state"] == "awaiting-review"
    assert task["pr_number"] == 44
    assert github.prs[0]["head"] == "codex/12-bounded-task"
    delivery = parse_delivery_block(github.prs[0]["body"])
    assert delivery.issue_number == 12
    assert delivery.task_hash == parse_task_body(body).task_hash
    assert delivery.context_commit == sha
    assert delivery.delivery_commit == git(tmp_path / "remote.git", "rev-parse", "codex/12-bounded-task")
    assert delivery.verification_passed is True
    assert delivery.model == "gpt-test"
    assert delivery.cli_version == "codex-test"
    assert delivery.acceptance_results[0]["criterion"] == "Unit tests pass"
    assert github.labels[-1] == ["priority:p1", "codex:awaiting-review"]
    assert git(tmp_path / "remote.git", "show", "codex/12-bounded-task:src/result.txt") == "implemented"
    assert len(store.list_runs("owner/repo", 12)) == 1
    assert preparation_profiles == ["codex-worker-preparation"]
    checkpoint = store.get_delivery_checkpoint(
        "owner/repo", 12, parse_task_body(body).task_hash
    )
    assert checkpoint is not None
    assert checkpoint["phase"] == "complete"
    assert checkpoint["retryable"] is False


def advance_remote_after_task_commit(
    tmp_path: Path,
    operations: GitOperations,
    remote: Path,
    relative_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_commit = operations.commit

    def commit_then_advance(*args: object, **kwargs: object) -> str:
        task_commit = original_commit(*args, **kwargs)
        upstream = tmp_path / "upstream-advance"
        git(tmp_path, "clone", str(remote), str(upstream))
        git(upstream, "config", "user.name", "Concurrent Developer")
        git(upstream, "config", "user.email", "developer@example.com")
        target = upstream / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("concurrent main change\n", encoding="utf-8")
        git(upstream, "add", ".")
        git(upstream, "commit", "-m", "advance main concurrently")
        git(upstream, "push", "origin", "main")
        return task_commit

    monkeypatch.setattr(operations, "commit", commit_then_advance)


def test_worker_integrates_advanced_non_overlapping_main_before_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, github, store, operations, issue = make_worker_fixture(tmp_path)
    remote = Path(config.repositories[0].clone_url)
    advance_remote_after_task_commit(
        tmp_path, operations, remote, "docs/concurrent.md", monkeypatch
    )
    verification_calls = 0
    real_verification = worker_module.run_verification

    def count_verification(*args: object, **kwargs: object) -> VerificationResult:
        nonlocal verification_calls
        verification_calls += 1
        return real_verification(*args, **kwargs)

    monkeypatch.setattr(worker_module, "run_verification", count_verification)

    service.process_issue(config.repositories[0], issue)

    task_hash = parse_task_body(issue["body"]).task_hash
    checkpoint = store.get_delivery_checkpoint("owner/repo", 12, task_hash)
    assert checkpoint is not None
    assert checkpoint["integration_refreshes"] == 1
    assert checkpoint["task_commit_sha"] != checkpoint["commit_sha"]
    assert checkpoint["integrated_base_sha"] == git(remote, "rev-parse", "main")
    assert len(operations.commit_parents(Path(checkpoint["worktree"]), checkpoint["commit_sha"])) == 2
    delivery = parse_delivery_block(github.prs[0]["body"])
    assert delivery.task_commit == checkpoint["task_commit_sha"]
    assert delivery.integrated_base == checkpoint["integrated_base_sha"]
    assert delivery.delivery_commit == checkpoint["commit_sha"]
    assert verification_calls == 2


def test_worker_stops_when_advanced_main_overlaps_task_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, github, store, operations, issue = make_worker_fixture(tmp_path)
    remote = Path(config.repositories[0].clone_url)
    advance_remote_after_task_commit(
        tmp_path, operations, remote, "src/result.txt", monkeypatch
    )

    service.process_issue(config.repositories[0], issue)

    task = store.get_task("owner/repo", 12)
    assert task is not None
    assert task["state"] == "needs-attention"
    assert github.prs == []
    assert any("overlap" in comment for comment in github.comments)


def test_retry_delivery_accepts_checkpointed_integration_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, store, operations, issue = make_worker_fixture(tmp_path)
    remote = Path(config.repositories[0].clone_url)
    advance_remote_after_task_commit(
        tmp_path, operations, remote, "docs/concurrent.md", monkeypatch
    )
    real_push = operations.push

    def fail_push(*args: object, **kwargs: object) -> None:
        raise GitError("connect timed out", retryable=True)

    monkeypatch.setattr(operations, "push", fail_push)
    service.process_issue(config.repositories[0], issue)
    monkeypatch.setattr(operations, "push", real_push)

    outcome = service.retry_delivery(config.repositories[0], issue)

    assert outcome == "awaiting-review"
    checkpoint = store.get_delivery_checkpoint(
        "owner/repo", 12, parse_task_body(issue["body"]).task_hash
    )
    assert checkpoint is not None
    assert checkpoint["integration_refreshes"] == 1


def test_automatic_mode_leaves_verified_draft_in_merging_state(
    tmp_path: Path,
) -> None:
    service, config, github, store, _, issue = make_worker_fixture(tmp_path)
    service.config = replace(config, merge_mode="automatic")

    service.process_issue(service.config.repositories[0], issue)

    task = store.get_task("owner/repo", 12)
    assert task is not None
    assert task["state"] == "merging"
    assert task["pr_number"] == 44
    assert github.labels[-1] == ["codex:merging"]


def test_auto_merge_delivery_adopts_checkpoint_without_invoking_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, store, _, issue = make_worker_fixture(tmp_path)
    service.process_issue(config.repositories[0], issue)
    task = store.get_task("owner/repo", 12)
    assert task is not None
    service.config = replace(config, merge_mode="automatic")

    class MustNotRun:
        def run(self, *args: object, **kwargs: object) -> RunnerResult:
            raise AssertionError("auto-merge adoption invoked Codex")

    service.runner = MustNotRun()
    captured: dict[str, object] = {}

    def fake_auto_merge(
        github: object,
        operation_store: EventStore,
        reference: object,
        *,
        pr_number: int,
        expected_head: str,
        merge_mode: str,
    ) -> AutomaticMergeResult:
        captured.update(
            pr_number=pr_number,
            expected_head=expected_head,
            merge_mode=merge_mode,
        )
        return AutomaticMergeResult(
            repo="owner/repo",
            issue_number=12,
            pr_number=pr_number,
            approved_head=expected_head,
            merge_commit_sha="e" * 40,
            merged=True,
        )

    monkeypatch.setattr(worker_module, "automatic_merge_task", fake_auto_merge)

    outcome = service.auto_merge_delivery(
        service.config.repositories[0], issue, task
    )

    checkpoint = store.get_delivery_checkpoint(
        "owner/repo", 12, parse_task_body(issue["body"]).task_hash
    )
    assert checkpoint is not None
    assert outcome == "completed"
    assert captured == {
        "pr_number": 44,
        "expected_head": checkpoint["commit_sha"],
        "merge_mode": "automatic",
    }


def test_auto_merge_delivery_refreshes_advanced_main_without_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, github, store, _, issue = make_worker_fixture(tmp_path)
    service.process_issue(config.repositories[0], issue)
    task = store.get_task("owner/repo", 12)
    assert task is not None
    service.config = replace(config, merge_mode="automatic")
    remote = Path(config.repositories[0].clone_url)
    upstream = tmp_path / "auto-merge-upstream"
    git(tmp_path, "clone", str(remote), str(upstream))
    git(upstream, "config", "user.name", "Concurrent Developer")
    git(upstream, "config", "user.email", "developer@example.com")
    (upstream / "docs" / "concurrent.md").write_text("new main\n", encoding="utf-8")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "advance before auto merge")
    git(upstream, "push", "origin", "main")
    captured_head = ""

    def fake_auto_merge(
        github_port: object,
        operation_store: EventStore,
        reference: object,
        *,
        pr_number: int,
        expected_head: str,
        merge_mode: str,
    ) -> AutomaticMergeResult:
        nonlocal captured_head
        captured_head = expected_head
        return AutomaticMergeResult(
            "owner/repo", 12, pr_number, expected_head, "e" * 40, True
        )

    monkeypatch.setattr(worker_module, "automatic_merge_task", fake_auto_merge)

    assert service.auto_merge_delivery(config.repositories[0], issue, task) == "completed"

    checkpoint = store.get_delivery_checkpoint(
        "owner/repo", 12, parse_task_body(issue["body"]).task_hash
    )
    assert checkpoint is not None
    assert checkpoint["integration_refreshes"] == 1
    assert checkpoint["commit_sha"] == captured_head
    assert git(remote, "rev-parse", "codex/12-bounded-task") == captured_head
    delivery = parse_delivery_block(github.updated_prs[-1]["body"])
    assert delivery.delivery_commit == captured_head
    assert delivery.integrated_base == git(remote, "rev-parse", "main")


def test_auto_merge_delivery_bounds_transient_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, store, _, issue = make_worker_fixture(tmp_path)
    service.process_issue(config.repositories[0], issue)
    task = store.get_task("owner/repo", 12)
    assert task is not None
    service.config = replace(config, merge_mode="automatic")

    def transient(*args: object, **kwargs: object) -> AutomaticMergeResult:
        raise GitHubError("temporary", status_code=503, retryable=True)

    monkeypatch.setattr(worker_module, "automatic_merge_task", transient)

    assert service.auto_merge_delivery(config.repositories[0], issue, task) == "merging"
    assert (
        service.auto_merge_delivery(config.repositories[0], issue, task)
        == "needs-attention"
    )


def test_auto_merge_delivery_does_not_retry_policy_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, _, _, issue = make_worker_fixture(tmp_path)
    service.process_issue(config.repositories[0], issue)
    task = service.store.get_task("owner/repo", 12)
    assert task is not None
    service.config = replace(config, merge_mode="automatic")

    def blocked(*args: object, **kwargs: object) -> AutomaticMergeResult:
        raise AutoMergeBlocked("unsafe Ruleset")

    monkeypatch.setattr(worker_module, "automatic_merge_task", blocked)

    assert (
        service.auto_merge_delivery(config.repositories[0], issue, task)
        == "needs-attention"
    )


def test_transient_push_failure_persists_retryable_delivery_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, store, operations, issue = make_worker_fixture(tmp_path)

    def fail_push(*args: object, **kwargs: object) -> None:
        raise GitError("connect timed out", retryable=True)

    monkeypatch.setattr(operations, "push", fail_push)

    service.process_issue(config.repositories[0], issue)

    task = store.get_task("owner/repo", 12)
    assert task is not None and task["state"] == "needs-attention"
    checkpoint = store.get_delivery_checkpoint(
        "owner/repo", 12, parse_task_body(issue["body"]).task_hash
    )
    assert checkpoint is not None
    assert checkpoint["commit_sha"] == git(Path(task["worktree"]), "rev-parse", "HEAD")
    assert checkpoint["phase"] == "push"
    assert checkpoint["retryable"] is True
    assert checkpoint["model"] == "gpt-test"


def test_crash_after_checkpoint_creation_leaves_delivery_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, store, _, issue = make_worker_fixture(tmp_path)

    def crash_before_delivery(*args: object, **kwargs: object) -> dict:
        raise SystemExit("simulated crash after checkpoint")

    monkeypatch.setattr(service, "_deliver_checkpoint", crash_before_delivery)

    with pytest.raises(SystemExit, match="simulated crash after checkpoint"):
        service.process_issue(config.repositories[0], issue)

    task_hash = parse_task_body(issue["body"]).task_hash
    checkpoint = store.get_delivery_checkpoint("owner/repo", 12, task_hash)
    assert checkpoint is not None
    assert checkpoint["phase"] == "delivery-ready"
    assert checkpoint["retryable"] is True


def test_permanent_push_failure_clears_delivery_retry_eligibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, store, operations, issue = make_worker_fixture(tmp_path)

    def fail_push(*args: object, **kwargs: object) -> None:
        raise GitError("authentication failed", retryable=False)

    monkeypatch.setattr(operations, "push", fail_push)

    service.process_issue(config.repositories[0], issue)

    checkpoint = store.get_delivery_checkpoint(
        "owner/repo", 12, parse_task_body(issue["body"]).task_hash
    )
    assert checkpoint is not None
    assert checkpoint["phase"] == "push"
    assert checkpoint["retryable"] is False


def test_transient_pr_failure_preserves_delivery_retry_eligibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, github, store, _, issue = make_worker_fixture(tmp_path)

    def fail_pr(*args: object, **kwargs: object) -> dict:
        raise GitHubError("service unavailable", status_code=503, retryable=True)

    monkeypatch.setattr(github, "create_draft_pr", fail_pr)

    service.process_issue(config.repositories[0], issue)

    checkpoint = store.get_delivery_checkpoint(
        "owner/repo", 12, parse_task_body(issue["body"]).task_hash
    )
    assert checkpoint is not None
    assert checkpoint["phase"] == "pull-request"
    assert checkpoint["retryable"] is True


def prepare_transient_push_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[WorkerService, WorkerConfig, FakeGitHub, EventStore, dict]:
    service, config, github, store, operations, issue = make_worker_fixture(tmp_path)
    real_push = operations.push

    def fail_push(*args: object, **kwargs: object) -> None:
        raise GitError("connect timed out", retryable=True)

    monkeypatch.setattr(operations, "push", fail_push)
    service.process_issue(config.repositories[0], issue)
    monkeypatch.setattr(operations, "push", real_push)
    return service, config, github, store, issue


def test_retry_delivery_reuses_checkpoint_without_running_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, store, issue = prepare_transient_push_failure(
        tmp_path, monkeypatch
    )
    task_hash = parse_task_body(issue["body"]).task_hash
    original = store.get_delivery_checkpoint("owner/repo", 12, task_hash)
    assert original is not None and original["retryable"] is True

    class MustNotRun:
        def run(self, *args: object, **kwargs: object) -> RunnerResult:
            raise AssertionError("delivery retry invoked Codex")

    service.runner = MustNotRun()

    outcome = service.retry_delivery(config.repositories[0], issue)

    assert outcome == "awaiting-review"
    task = store.get_task("owner/repo", 12)
    assert task is not None and task["pr_number"] == 44
    assert git(tmp_path / "remote.git", "rev-parse", "codex/12-bounded-task") == original[
        "commit_sha"
    ]
    completed = store.get_delivery_checkpoint("owner/repo", 12, task_hash)
    assert completed is not None and completed["retryable"] is False


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ("task-body", "task body changed"),
        ("branch", "branch changed"),
        ("head", "HEAD changed"),
        ("dirty", "worktree is not clean"),
    ],
)
def test_retry_delivery_rejects_integrity_drift_before_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected: str,
) -> None:
    service, config, github, store, issue = prepare_transient_push_failure(
        tmp_path, monkeypatch
    )
    task = store.get_task("owner/repo", 12)
    assert task is not None
    worktree = Path(task["worktree"])
    if mutation == "task-body":
        issue["body"] = issue["body"].replace("Unit tests pass", "Changed criterion")
    elif mutation == "branch":
        git(worktree, "switch", "-c", "unexpected-branch")
    elif mutation == "head":
        git(worktree, "config", "user.name", "Test")
        git(worktree, "config", "user.email", "test@example.com")
        git(worktree, "commit", "--allow-empty", "-m", "unexpected")
    elif mutation == "dirty":
        (worktree / "src" / "result.txt").write_text("dirty\n", encoding="utf-8")
    else:
        raise AssertionError(f"unknown mutation: {mutation}")
    pushed = False

    def forbidden_push(*args: object, **kwargs: object) -> None:
        nonlocal pushed
        pushed = True

    monkeypatch.setattr(service.git, "push", forbidden_push)

    assert service.retry_delivery(config.repositories[0], issue) == "not-retryable"
    assert pushed is False
    assert expected in github.comments[-1]


def test_retry_delivery_rejects_multiple_parents_before_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, github, _, issue = prepare_transient_push_failure(
        tmp_path, monkeypatch
    )
    spec = parse_task_body(issue["body"])
    monkeypatch.setattr(
        service.git,
        "commit_parents",
        lambda worktree, commit_sha: (spec.context_commit, "4" * 40),
    )
    monkeypatch.setattr(
        service.git,
        "push",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("push called")),
    )

    assert service.retry_delivery(config.repositories[0], issue) == "not-retryable"
    assert "sole parent" in github.comments[-1]


def test_retry_delivery_rejects_project_config_hash_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, github, _, issue = prepare_transient_push_failure(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(service, "_project_config_hash", lambda worktree: "f" * 64)
    monkeypatch.setattr(
        service.git,
        "push",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("push called")),
    )

    assert service.retry_delivery(config.repositories[0], issue) == "not-retryable"
    assert "project config changed" in github.comments[-1]


def test_retry_delivery_stops_when_fresh_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, store, issue = prepare_transient_push_failure(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        worker_module,
        "run_verification",
        lambda *args, **kwargs: VerificationResult(
            False,
            (CommandResult("pytest", 1, "failed"),),
        ),
    )
    monkeypatch.setattr(
        service.git,
        "push",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("push called")),
    )

    assert service.retry_delivery(config.repositories[0], issue) == "not-retryable"
    checkpoint = store.get_delivery_checkpoint(
        "owner/repo", 12, parse_task_body(issue["body"]).task_hash
    )
    assert checkpoint is not None and checkpoint["retryable"] is False


def test_retry_delivery_pause_persists_verification_only_resume_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, store, issue = prepare_transient_push_failure(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        worker_module,
        "run_verification",
        lambda *args, **kwargs: VerificationResult(
            False,
            (),
            termination_reason="pause",
        ),
    )

    assert service.retry_delivery(config.repositories[0], issue) == "paused"

    task_hash = parse_task_body(issue["body"]).task_hash
    checkpoint = store.get_delivery_checkpoint("owner/repo", 12, task_hash)
    assert checkpoint is not None
    assert checkpoint["phase"] == "paused-verification"
    assert checkpoint["retryable"] is True


def test_transient_retry_preflight_failure_preserves_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, store, issue = prepare_transient_push_failure(
        tmp_path, monkeypatch
    )

    def fail_authority(*args: object, **kwargs: object) -> None:
        raise GitHubError("service unavailable", status_code=503, retryable=True)

    monkeypatch.setattr(service, "_validate_issue_author", fail_authority)

    assert service.retry_delivery(config.repositories[0], issue) == "needs-attention"

    task_hash = parse_task_body(issue["body"]).task_hash
    checkpoint = store.get_delivery_checkpoint("owner/repo", 12, task_hash)
    assert checkpoint is not None
    assert checkpoint["phase"] == "preflight"
    assert checkpoint["retryable"] is True


def test_transient_legacy_preflight_failure_does_not_reject_reconstruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, store, issue, _ = prepare_legacy_delivery(tmp_path)

    def fail_authority(*args: object, **kwargs: object) -> None:
        raise GitHubError("service unavailable", status_code=503, retryable=True)

    monkeypatch.setattr(service, "_validate_issue_author", fail_authority)

    assert service.retry_delivery(config.repositories[0], issue) == "needs-attention"

    task_hash = parse_task_body(issue["body"]).task_hash
    assert store.get_delivery_checkpoint("owner/repo", 12, task_hash) is None
    assert store.get_worker_state(
        f"legacy-delivery-recovery:owner/repo#12:{task_hash}"
    ) is None


def test_retry_delivery_rejects_expired_hard_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, _, issue = prepare_transient_push_failure(tmp_path, monkeypatch)
    monkeypatch.setattr(worker_module, "DELIVERY_RETRY_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(
        service.git,
        "push",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("push called")),
    )

    assert service.retry_delivery(config.repositories[0], issue) == "not-retryable"


def test_retry_delivery_deadline_stops_before_draft_pr_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, github, _, issue = prepare_transient_push_failure(
        tmp_path, monkeypatch
    )
    moments = iter((0.0, 0.0, 0.0, 2.0))
    last = 2.0

    def monotonic() -> float:
        nonlocal last
        try:
            last = next(moments)
        except StopIteration:
            pass
        return last

    monkeypatch.setattr(worker_module, "DELIVERY_RETRY_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(worker_module.time, "monotonic", monotonic)
    monkeypatch.setattr(service.git, "push", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        worker_module,
        "run_verification",
        lambda *args, **kwargs: VerificationResult(True, ()),
    )

    assert service.retry_delivery(config.repositories[0], issue) == "not-retryable"
    assert github.prs == []


def test_retry_delivery_scopes_all_github_requests_to_hard_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, _, issue = prepare_transient_push_failure(
        tmp_path, monkeypatch
    )

    class DeadlineAwareGitHub(FakeGitHub):
        def __init__(self, current_issue: dict) -> None:
            super().__init__(current_issue)
            self.active_deadline: float | None = None
            self.seen_deadlines: list[float] = []

        @contextmanager
        def request_deadline(self, deadline_monotonic: float):
            self.active_deadline = deadline_monotonic
            self.seen_deadlines.append(deadline_monotonic)
            try:
                yield
            finally:
                self.active_deadline = None

        def get_repository(self, repo: str) -> dict:
            assert self.active_deadline is not None
            return super().get_repository(repo)

        def create_draft_pr(
            self, repo: str, head: str, base: str, title: str, body: str
        ) -> dict:
            assert self.active_deadline is not None
            return super().create_draft_pr(repo, head, base, title, body)

    github = DeadlineAwareGitHub(issue)
    service.github = github

    assert service.retry_delivery(config.repositories[0], issue) == "awaiting-review"
    assert len(github.seen_deadlines) == 1


def test_delivery_phase_rechecks_deadline_after_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, github, store, issue = prepare_transient_push_failure(
        tmp_path, monkeypatch
    )
    task_hash = parse_task_body(issue["body"]).task_hash
    checkpoint = store.get_delivery_checkpoint("owner/repo", 12, task_hash)
    assert checkpoint is not None
    monkeypatch.setattr(service.git, "push", lambda *args, **kwargs: None)
    moments = iter((0.0, 2.0))
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: next(moments))

    with pytest.raises(RunnerTimeout, match="before pull request"):
        service._deliver_checkpoint(
            config.repositories[0],
            issue,
            parse_task_body(issue["body"]),
            checkpoint,
            VerificationResult(True, ()),
            status_comment_id=1,
            deadline_monotonic=1.0,
        )

    assert github.prs == []


def test_delivery_finalization_rechecks_deadline_between_github_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, store, issue = prepare_transient_push_failure(
        tmp_path, monkeypatch
    )
    task_hash = parse_task_body(issue["body"]).task_hash
    checkpoint = store.get_delivery_checkpoint("owner/repo", 12, task_hash)
    assert checkpoint is not None
    monkeypatch.setattr(service.git, "push", lambda *args, **kwargs: None)
    moments = iter((0.0, 0.0, 0.0, 2.0))
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: next(moments))

    with pytest.raises(RunnerTimeout, match="before status comment"):
        service._deliver_checkpoint(
            config.repositories[0],
            issue,
            parse_task_body(issue["body"]),
            checkpoint,
            VerificationResult(True, ()),
            status_comment_id=1,
            deadline_monotonic=1.0,
        )

    task = store.get_task("owner/repo", 12)
    assert task is not None
    assert task["state"] != "awaiting-review"


def test_retry_delivery_reconciles_existing_pr_after_ambiguous_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, store, issue = prepare_transient_push_failure(
        tmp_path, monkeypatch
    )

    class AmbiguousRemote(FakeGitHub):
        def __init__(self, current_issue: dict) -> None:
            super().__init__(current_issue)
            self.existing: dict | None = None
            self.create_calls = 0

        def find_open_pull_request(self, repo: str, head: str) -> dict | None:
            if self.existing and self.existing["head"] == head:
                return self.existing
            return None

        def create_draft_pr(
            self,
            repo: str,
            head: str,
            base: str,
            title: str,
            body: str,
        ) -> dict:
            self.create_calls += 1
            self.existing = super().create_draft_pr(repo, head, base, title, body)
            raise GitHubError("response lost", status_code=None, retryable=True)

    remote = AmbiguousRemote(issue)
    service.github = DurableGitHub(remote, store)

    assert service.retry_delivery(config.repositories[0], issue) == "needs-attention"
    assert service.retry_delivery(config.repositories[0], issue) == "awaiting-review"
    assert remote.create_calls == 1
    assert len(remote.prs) == 1


def prepare_legacy_delivery(
    tmp_path: Path,
    *,
    damage: str | None = None,
) -> tuple[WorkerService, WorkerConfig, FakeGitHub, EventStore, dict, str]:
    service, config, github, store, operations, issue = make_worker_fixture(tmp_path)
    spec = parse_task_body(issue["body"])
    mirror = operations.ensure_mirror(
        "owner/repo",
        config.repositories[0].clone_url,
        token="token",
    )
    prepared = operations.prepare_worktree(
        repo="owner/repo",
        mirror=mirror,
        context_commit=spec.context_commit,
        base_branch=spec.base_branch,
        issue_number=12,
        slug="bounded-task",
    )
    result = FakeRunner().run(
        prepared.path,
        "prompt",
        tmp_path / "schema.json",
    )
    if damage == "scope-violation":
        (prepared.path / ".env").write_text(
            'PASSWORD="abcdefghijklmnop"\n',
            encoding="utf-8",
        )
    if damage != "missing-run":
        run_id = store.start_run("owner/repo", 12)
        store.finish_run(
            run_id,
            exit_code=1 if damage == "nonzero-run" else 0,
            result={
                "session_id": result.session_id,
                "termination_reason": result.termination_reason,
                "event_count": len(result.events),
                "last_message": (
                    "{" if damage == "invalid-final-message" else result.last_message
                ),
                "model": result.model,
                "cli_version": result.cli_version,
            },
        )
    commit_sha = operations.commit(
        prepared.path,
        "feat: complete codex task #12",
        author_name="Codex Mac Worker",
        author_email="codex-worker@users.noreply.github.com",
    )
    if damage == "wrong-parent":
        git(prepared.path, "commit", "--allow-empty", "-m", "second delivery commit")
        commit_sha = git(prepared.path, "rev-parse", "HEAD")
    store.upsert_task(
        repo="owner/repo",
        issue_number=12,
        task_hash=spec.task_hash,
        state="needs-attention",
        branch=prepared.branch,
        worktree=str(prepared.path),
        session_id=(
            "different-session" if damage == "session-mismatch" else result.session_id
        ),
    )
    if damage == "dirty-worktree":
        (prepared.path / "src" / "result.txt").write_text("dirty\n", encoding="utf-8")
    elif damage == "wrong-branch":
        git(prepared.path, "switch", "-c", "unexpected-branch")
    elif damage == "missing-worktree":
        prepared.path.rename(prepared.path.with_name(prepared.path.name + "-moved"))
    return service, config, github, store, issue, commit_sha


def test_retry_delivery_reconstructs_strict_legacy_checkpoint_once(
    tmp_path: Path,
) -> None:
    service, config, _, store, issue, commit_sha = prepare_legacy_delivery(tmp_path)
    task_hash = parse_task_body(issue["body"]).task_hash

    outcome = service.retry_delivery(config.repositories[0], issue)

    assert outcome == "awaiting-review"
    checkpoint = store.get_delivery_checkpoint("owner/repo", 12, task_hash)
    assert checkpoint is not None
    assert checkpoint["commit_sha"] == commit_sha
    assert store.get_worker_state(
        f"legacy-delivery-recovery:owner/repo#12:{task_hash}"
    ) == "reconstructed"
    assert git(tmp_path / "remote.git", "rev-parse", "codex/12-bounded-task") == commit_sha


@pytest.mark.parametrize(
    "damage",
    [
        "missing-worktree",
        "dirty-worktree",
        "wrong-branch",
        "wrong-parent",
        "missing-run",
        "nonzero-run",
        "session-mismatch",
        "invalid-final-message",
        "scope-violation",
    ],
)
def test_legacy_reconstruction_rejects_incomplete_evidence(
    tmp_path: Path,
    damage: str,
) -> None:
    service, config, github, store, issue, _ = prepare_legacy_delivery(
        tmp_path,
        damage=damage,
    )
    task_hash = parse_task_body(issue["body"]).task_hash

    outcome = service.retry_delivery(config.repositories[0], issue)

    assert outcome == "not-retryable"
    assert store.get_delivery_checkpoint("owner/repo", 12, task_hash) is None
    assert store.get_worker_state(
        f"legacy-delivery-recovery:owner/repo#12:{task_hash}"
    ) == "rejected"
    assert github.prs == []


def test_legacy_reconstruction_rejects_fresh_verification_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, config, _, store, issue, _ = prepare_legacy_delivery(tmp_path)
    task_hash = parse_task_body(issue["body"]).task_hash
    monkeypatch.setattr(
        worker_module,
        "run_verification",
        lambda *args, **kwargs: VerificationResult(
            False,
            (CommandResult("pytest", 1, "failed"),),
        ),
    )

    assert service.retry_delivery(config.repositories[0], issue) == "not-retryable"
    assert store.get_delivery_checkpoint("owner/repo", 12, task_hash) is None
    assert store.get_worker_state(
        f"legacy-delivery-recovery:owner/repo#12:{task_hash}"
    ) == "rejected"


def test_worker_rejects_unauthorized_issue_author(tmp_path: Path) -> None:
    remote, sha = make_project_remote(tmp_path)
    issue = {
        "number": 14,
        "title": "Untrusted task",
        "body": task_body(sha=sha),
        "labels": [{"name": "codex:queued"}],
        "user": {"login": "outsider"},
    }
    github = FakeGitHub(issue)
    config = WorkerConfig(
        "mac-mini", 60, 120, tmp_path / "state.sqlite3", tmp_path / "cache",
        tmp_path / "worktrees", tmp_path / "outputs", Path("/tmp/codex"), "123", "456",
        tmp_path / "app.pem", ("owner",), (RepositoryConfig("owner/repo", str(remote)),),
    )
    store = EventStore(config.database_path)
    service = WorkerService(
        config=config,
        github=github,
        token_provider=lambda: "token",
        store=store,
        git=GitOperations(cache_root=config.cache_root, worktree_root=config.worktree_root),
        runner=FakeRunner(),
    )

    service.process_issue(config.repositories[0], issue)

    assert store.get_task("owner/repo", 14)["state"] == "needs-attention"
    assert github.prs == []
    assert "not authorized" in github.comments[-1]


def test_worker_rejects_task_after_trusted_app_rotation(tmp_path: Path) -> None:
    remote, sha = make_project_remote(tmp_path)
    issue = {
        "number": 17,
        "title": "Stale Worker authority",
        "body": task_body(sha=sha),
        "labels": [{"name": "codex:queued"}],
        "user": {"login": "owner"},
    }
    github = FakeGitHub(issue)
    github.current_project_config = project_config_text(worker_github_app_id=999)
    config = WorkerConfig(
        "mac-mini", 60, 120, tmp_path / "state.sqlite3", tmp_path / "cache",
        tmp_path / "worktrees", tmp_path / "outputs", Path("/tmp/codex"), "123", "456",
        tmp_path / "app.pem", ("owner",), (RepositoryConfig("owner/repo", str(remote)),),
    )
    store = EventStore(config.database_path)
    service = WorkerService(
        config=config,
        github=github,
        token_provider=lambda: "token",
        store=store,
        git=GitOperations(cache_root=config.cache_root, worktree_root=config.worktree_root),
        runner=FakeRunner(),
    )

    service.process_issue(config.repositories[0], issue)

    assert store.get_task("owner/repo", 17)["state"] == "needs-attention"
    assert github.prs == []
    assert "trusted GitHub App" in github.comments[-1]


def test_worker_treats_structured_blocked_result_as_attention(tmp_path: Path) -> None:
    remote, sha = make_project_remote(tmp_path)
    issue = {
        "number": 15,
        "title": "Blocked task",
        "body": task_body(sha=sha),
        "labels": [{"name": "codex:queued"}],
        "user": {"login": "owner"},
    }
    github = FakeGitHub(issue)

    class BlockedRunner(FakeRunner):
        def run(self, worktree: Path, prompt: str, output_schema: Path, **kwargs: object) -> RunnerResult:
            (worktree / "src" / "result.txt").write_text("partial\n", encoding="utf-8")
            return RunnerResult(
                0,
                "session-blocked",
                (),
                '{"status":"blocked","summary":"need input","changed_files":[],"risks":[],"needs_human":["choose"]}',
                "",
            )

    config = WorkerConfig(
        "mac-mini", 60, 120, tmp_path / "state.sqlite3", tmp_path / "cache",
        tmp_path / "worktrees", tmp_path / "outputs", Path("/tmp/codex"), "123", "456",
        tmp_path / "app.pem", ("owner",), (RepositoryConfig("owner/repo", str(remote)),),
    )
    store = EventStore(config.database_path)
    service = WorkerService(
        config=config,
        github=github,
        token_provider=lambda: "token",
        store=store,
        git=GitOperations(cache_root=config.cache_root, worktree_root=config.worktree_root),
        runner=BlockedRunner(),
    )

    service.process_issue(config.repositories[0], issue)

    assert store.get_task("owner/repo", 15)["state"] == "needs-attention"
    assert github.prs == []
    assert "reported blocked" in github.comments[-1]


def test_worker_revalidates_diff_after_verification_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, sha = make_project_remote(tmp_path)
    issue = {
        "number": 16,
        "title": "Verification mutation",
        "body": task_body(sha=sha),
        "labels": [{"name": "codex:queued"}],
        "user": {"login": "owner"},
    }
    github = FakeGitHub(issue)

    def mutating_verification(worktree: Path, *args: object, **kwargs: object) -> VerificationResult:
        (worktree / ".env").write_text('PASSWORD="abcdefghijklmnop"\n', encoding="utf-8")
        return VerificationResult(True, ())

    monkeypatch.setattr("codex_mac_worker.worker.run_verification", mutating_verification)
    config = WorkerConfig(
        "mac-mini", 60, 120, tmp_path / "state.sqlite3", tmp_path / "cache",
        tmp_path / "worktrees", tmp_path / "outputs", Path("/tmp/codex"), "123", "456",
        tmp_path / "app.pem", ("owner",), (RepositoryConfig("owner/repo", str(remote)),),
    )
    store = EventStore(config.database_path)
    service = WorkerService(
        config=config,
        github=github,
        token_provider=lambda: "token",
        store=store,
        git=GitOperations(cache_root=config.cache_root, worktree_root=config.worktree_root),
        runner=FakeRunner(),
    )

    service.process_issue(config.repositories[0], issue)

    assert store.get_task("owner/repo", 16)["state"] == "needs-attention"
    assert github.prs == []
    assert "protected" in github.comments[-1]


def test_worker_rejects_repository_codex_config_that_could_override_permissions(
    tmp_path: Path,
) -> None:
    remote, sha = make_project_remote(tmp_path)
    source = tmp_path / "inject"
    git(tmp_path, "clone", str(remote), str(source))
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    (source / ".codex").mkdir()
    (source / ".codex" / "config.toml").write_text(
        'sandbox_mode = "danger-full-access"\n', encoding="utf-8"
    )
    git(source, "add", ".codex/config.toml")
    git(source, "commit", "-m", "add unsafe project codex config")
    git(source, "push", "origin", "HEAD:main")
    sha = git(source, "rev-parse", "HEAD")
    issue = {
        "number": 13,
        "title": "Unsafe config",
        "body": task_body(sha=sha),
        "labels": [{"name": "codex:queued"}],
        "user": {"login": "owner"},
    }
    github = FakeGitHub(issue)
    config = WorkerConfig(
        "mac-mini", 60, 120, tmp_path / "state.sqlite3", tmp_path / "cache",
        tmp_path / "worktrees", tmp_path / "outputs", Path("/tmp/codex"), "123", "456",
        tmp_path / "app.pem", ("owner",), (RepositoryConfig("owner/repo", str(remote)),),
    )
    store = EventStore(config.database_path)
    service = WorkerService(
        config=config,
        github=github,
        token_provider=lambda: "token",
        store=store,
        git=GitOperations(cache_root=config.cache_root, worktree_root=config.worktree_root),
        runner=FakeRunner(),
    )

    service.process_issue(config.repositories[0], issue)

    assert store.get_task("owner/repo", 13)["state"] == "needs-attention"
    assert github.prs == []
    assert "project Codex config" in github.comments[-1]


def test_worker_rejects_task_body_changed_after_claim(tmp_path: Path) -> None:
    remote, sha = make_project_remote(tmp_path)
    issue = {
        "number": 12,
        "title": "Bounded task",
        "body": task_body(sha=sha),
        "labels": [{"name": "codex:queued"}],
        "user": {"login": "owner"},
    }
    github = FakeGitHub(issue)

    class MutatingRunner(FakeRunner):
        def run(self, worktree: Path, prompt: str, output_schema: Path, **kwargs: object) -> RunnerResult:
            result = super().run(worktree, prompt, output_schema, **kwargs)
            github.issue["body"] = github.issue["body"].replace("Unit tests pass", "Different acceptance")
            return result

    config = WorkerConfig(
        "mac-mini", 60, 120, tmp_path / "state.sqlite3", tmp_path / "cache",
        tmp_path / "worktrees", tmp_path / "outputs", Path("/tmp/codex"), "123", "456",
        tmp_path / "app.pem", ("owner",), (RepositoryConfig("owner/repo", str(remote)),),
    )
    store = EventStore(config.database_path)
    service = WorkerService(
        config=config,
        github=github,
        token_provider=lambda: "token",
        store=store,
        git=GitOperations(cache_root=config.cache_root, worktree_root=config.worktree_root),
        runner=MutatingRunner(),
    )

    service.process_issue(config.repositories[0], issue)

    assert store.get_task("owner/repo", 12)["state"] == "needs-attention"
    assert github.prs == []


def test_worker_revises_existing_branch_without_creating_second_pr(tmp_path: Path) -> None:
    remote, sha = make_project_remote(tmp_path)
    issue = {
        "number": 12,
        "title": "Bounded task",
        "body": task_body(sha=sha),
        "labels": [{"name": "codex:queued"}],
        "user": {"login": "owner"},
    }
    github = FakeGitHub(issue)
    config = WorkerConfig(
        "mac-mini", 60, 120, tmp_path / "state.sqlite3", tmp_path / "cache",
        tmp_path / "worktrees", tmp_path / "outputs", Path("/tmp/codex"), "123", "456",
        tmp_path / "app.pem", ("owner",), (RepositoryConfig("owner/repo", str(remote)),),
    )
    store = EventStore(config.database_path)
    service = WorkerService(
        config=config,
        github=github,
        token_provider=lambda: "token",
        store=store,
        git=GitOperations(cache_root=config.cache_root, worktree_root=config.worktree_root),
        runner=FakeRunner(),
    )
    service.process_issue(config.repositories[0], issue)
    task = store.get_task("owner/repo", 12)
    assert task is not None

    class RevisionRunner:
        def run(
            self, worktree: Path, prompt: str, output_schema: Path, **kwargs: object
        ) -> RunnerResult:
            assert "Rename the result" in prompt
            (worktree / "src" / "result.txt").write_text("revised\n", encoding="utf-8")
            return RunnerResult(
                0,
                "session-2",
                (),
                '{"status":"completed","summary":"revised","changed_files":["src/result.txt"],'
                '"acceptance_results":[{"criterion":"Unit tests pass","status":"met",'
                '"evidence":"fast verification after revision"}],"risks":[],"needs_human":[]}',
                "",
                model="gpt-revision",
                cli_version="codex-revision",
            )

    service.runner = RevisionRunner()
    service.revise_issue(
        config.repositories[0],
        issue,
        task,
        ("Rename the result",),
    )

    assert store.get_task("owner/repo", 12)["state"] == "awaiting-review"
    assert len(github.prs) == 1
    assert len(github.updated_prs) == 1
    revised_delivery = parse_delivery_block(github.updated_prs[0]["body"])
    assert revised_delivery.delivery_commit == git(
        tmp_path / "remote.git", "rev-parse", "codex/12-bounded-task"
    )
    assert revised_delivery.model == "gpt-revision"
    assert git(tmp_path / "remote.git", "show", "codex/12-bounded-task:src/result.txt") == "revised"
    assert git(tmp_path / "remote.git", "rev-list", "--count", "codex/12-bounded-task") == "3"
    assert len(store.list_runs("owner/repo", 12)) == 2

    github.current_project_config = project_config_text(worker_github_app_id=999)
    service.revise_issue(
        config.repositories[0],
        issue,
        store.get_task("owner/repo", 12),
        ("Another revision",),
    )
    assert store.get_task("owner/repo", 12)["state"] == "needs-attention"
    assert len(github.updated_prs) == 1


def test_worker_restart_cannot_extend_original_hard_deadline(tmp_path: Path) -> None:
    remote, sha = make_project_remote(tmp_path)
    issue = {
        "number": 12,
        "title": "Bounded task",
        "body": task_body(sha=sha),
        "labels": [{"name": "codex:queued"}],
        "user": {"login": "owner"},
    }
    github = FakeGitHub(issue)
    config = WorkerConfig(
        "mac-mini", 60, 120, tmp_path / "state.sqlite3", tmp_path / "cache",
        tmp_path / "worktrees", tmp_path / "outputs", Path("/tmp/codex"), "123", "456",
        tmp_path / "app.pem", ("owner",), (RepositoryConfig("owner/repo", str(remote)),),
    )
    store = EventStore(config.database_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=12,
        task_hash="old",
        state="running",
        branch="codex/12-bounded-task",
    )
    store.connection.execute(
        "UPDATE tasks SET claimed_at='2020-01-01T00:00:00+00:00' WHERE repo=? AND issue_number=?",
        ("owner/repo", 12),
    )
    store.connection.commit()

    called = False

    class MustNotRun:
        def run(self, *args: object, **kwargs: object) -> RunnerResult:
            nonlocal called
            called = True
            return RunnerResult(0, "unexpected", (), "{}", "")

    service = WorkerService(
        config=config,
        github=github,
        token_provider=lambda: "token",
        store=store,
        git=GitOperations(cache_root=config.cache_root, worktree_root=config.worktree_root),
        runner=MustNotRun(),
    )

    service.process_issue(config.repositories[0], issue)

    assert store.get_task("owner/repo", 12)["state"] == "needs-attention"
    assert called is False
    assert "hard timeout" in github.comments[-1]
