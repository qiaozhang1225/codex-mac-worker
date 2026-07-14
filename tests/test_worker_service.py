from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from codex_mac_worker.config import RepositoryConfig, WorkerConfig
from codex_mac_worker.gitops import GitOperations
from codex_mac_worker.protocol import parse_delivery_block, parse_task_body
from codex_mac_worker.runner import RunnerResult
from codex_mac_worker.store import EventStore
from codex_mac_worker.verification import VerificationResult
from codex_mac_worker.worker import WorkerService

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


def test_worker_processes_bounded_task_into_draft_pr(tmp_path: Path) -> None:
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
