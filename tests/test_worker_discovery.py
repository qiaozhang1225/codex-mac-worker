from __future__ import annotations

import hashlib
from pathlib import Path

from codex_mac_worker.config import RepositoryConfig, WorkerConfig
from codex_mac_worker.daemon import WorkerDaemon
from codex_mac_worker.github import GitHubError
from codex_mac_worker.protocol import render_repository_probe
from codex_mac_worker.store import EventStore
from codex_mac_worker.worker import WorkerService


PROJECT_TOML = """
schema_version = 2
default_base_branch = "main"
worker_github_app_id = 123
allowed_risk_levels = ["low", "medium"]
protected_paths = [".codex-worker", ".github/workflows", ".env"]
max_changed_files = 10
max_diff_lines = 100
codex_attempt_timeout_minutes = 45
task_hard_timeout_minutes = 120
max_automatic_attempts = 2
[verification.fast]
commands = ["python -m unittest"]
""".strip() + "\n"


def worker_config(
    tmp_path: Path,
    *,
    repositories: tuple[RepositoryConfig, ...] = (),
    discover: bool = True,
) -> WorkerConfig:
    return WorkerConfig(
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
        repositories=repositories,
        discover_installation_repositories=discover,
    )


class DiscoveryGitHub:
    def __init__(self) -> None:
        self.installation_calls = 0

    def list_installation_repositories(self) -> list[dict]:
        self.installation_calls += 1
        return [
            {
                "full_name": "owner/ready",
                "clone_url": "https://github.com/owner/ready.git",
                "default_branch": "main",
            },
            {
                "full_name": "owner/missing",
                "clone_url": "https://github.com/owner/missing.git",
                "default_branch": "main",
            },
            {
                "full_name": "owner/mismatch",
                "clone_url": "https://github.com/owner/mismatch.git",
                "default_branch": "develop",
            },
            {
                "full_name": "owner/wrong-app",
                "clone_url": "https://github.com/owner/wrong-app.git",
                "default_branch": "main",
            },
            {
                "full_name": "owner/legacy-v1",
                "clone_url": "https://github.com/owner/legacy-v1.git",
                "default_branch": "main",
            },
        ]

    def get_repository_file(self, repo: str, path: str, *, ref: str) -> str:
        assert path == ".codex-worker/project.toml"
        if repo == "owner/missing":
            raise GitHubError("missing", status_code=404, retryable=False)
        if repo == "owner/wrong-app":
            return PROJECT_TOML.replace(
                "worker_github_app_id = 123", "worker_github_app_id = 999"
            )
        if repo == "owner/legacy-v1":
            return PROJECT_TOML.replace("schema_version = 2", "schema_version = 1")
        return PROJECT_TOML

    def list_queued_issues(self, repo: str) -> list[dict]:
        return []


class NoopService:
    def process_issue(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("no normal task should run")

    def revise_issue(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("no revision should run")


def test_daemon_discovers_only_installed_repositories_with_valid_project_config(
    tmp_path: Path,
) -> None:
    github = DiscoveryGitHub()
    settings = worker_config(tmp_path)
    daemon = WorkerDaemon(
        settings,
        github,
        EventStore(settings.database_path),
        NoopService(),
    )

    first = daemon.repositories()
    second = daemon.repositories()

    assert [repo.name for repo in first] == ["owner/ready"]
    assert second == first
    assert github.installation_calls == 1


class ProbeGitHub:
    def __init__(self, config_text: str) -> None:
        self.config_text = config_text
        self.comments: list[str] = []
        self.updates: list[dict] = []

    def collaborator_permission(self, repo: str, username: str) -> str:
        return "write"

    def get_repository(self, repo: str) -> dict:
        return {"default_branch": "main"}

    def get_commit(self, repo: str, ref: str) -> dict:
        assert ref == "main"
        return {"sha": "a" * 40}

    def get_repository_file(self, repo: str, path: str, *, ref: str) -> str:
        assert path == ".codex-worker/project.toml"
        assert ref == "a" * 40
        return self.config_text

    def add_comment(self, repo: str, issue_number: int, body: str) -> dict:
        self.comments.append(body)
        return {"id": 99}

    def update_issue(
        self,
        repo: str,
        issue_number: int,
        *,
        labels: list[str] | None = None,
        state: str | None = None,
    ) -> dict:
        update = {"labels": labels, "state": state}
        self.updates.append(update)
        return update


class RunnerMustNotRun:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def run(self, *args: object, **kwargs: object) -> None:
        self.calls.append((args, kwargs))
        raise AssertionError("repository probe must not invoke Codex")


def test_probe_is_attested_without_invoking_runner(tmp_path: Path) -> None:
    repository = RepositoryConfig("owner/ready", "https://github.com/owner/ready.git")
    settings = worker_config(tmp_path, repositories=(repository,))
    github = ProbeGitHub(PROJECT_TOML)
    runner = RunnerMustNotRun()
    issue = {
        "number": 7,
        "body": render_repository_probe(
            probe_id="probe-1",
            default_head="a" * 40,
            project_config_hash=hashlib.sha256(PROJECT_TOML.encode()).hexdigest(),
        ),
        "labels": [{"name": "codex:queued"}, {"name": "kind:probe"}],
        "user": {"login": "owner"},
    }
    service = WorkerService(
        config=settings,
        github=github,
        token_provider=lambda: "token",
        store=EventStore(settings.database_path),
        git=object(),
        runner=runner,
    )

    service.process_repository_probe(repository, issue)

    assert runner.calls == []
    assert "<!-- codex-worker-readiness:v1 -->" in github.comments[-1]
    assert github.updates == [
        {"labels": ["kind:probe", "codex:completed"], "state": "closed"}
    ]


def test_probe_rejects_project_bound_to_another_github_app(tmp_path: Path) -> None:
    repository = RepositoryConfig("owner/ready", "https://github.com/owner/ready.git")
    settings = worker_config(tmp_path, repositories=(repository,))
    project = PROJECT_TOML.replace(
        "worker_github_app_id = 123", "worker_github_app_id = 999"
    )
    github = ProbeGitHub(project)
    issue = {
        "number": 8,
        "body": render_repository_probe(
            probe_id="probe-wrong-app",
            default_head="a" * 40,
            project_config_hash=hashlib.sha256(project.encode()).hexdigest(),
        ),
        "labels": [{"name": "codex:queued"}],
        "user": {"login": "owner"},
    }
    service = WorkerService(
        config=settings,
        github=github,
        token_provider=lambda: "token",
        store=EventStore(settings.database_path),
        git=object(),
        runner=RunnerMustNotRun(),
    )

    service.process_repository_probe(repository, issue)

    assert github.comments
    assert "trusted GitHub App" in github.comments[-1]
    assert github.updates[-1]["labels"] == ["codex:needs-attention"]


def test_daemon_routes_probe_before_normal_task_processing(tmp_path: Path) -> None:
    repository = RepositoryConfig("owner/ready", "https://github.com/owner/ready.git")
    settings = worker_config(
        tmp_path,
        repositories=(repository,),
        discover=False,
    )
    issue = {
        "number": 7,
        "created_at": "2026-07-14T00:00:00Z",
        "body": render_repository_probe(
            probe_id="probe-1",
            default_head="a" * 40,
            project_config_hash="b" * 64,
        ),
    }

    class QueueGitHub:
        def list_queued_issues(self, repo: str) -> list[dict]:
            return [issue]

    class RoutingService:
        def __init__(self) -> None:
            self.probes: list[int] = []

        def validate_repository_authority(self, repository: RepositoryConfig) -> None:
            return None

        def process_repository_probe(self, repository: RepositoryConfig, queued: dict) -> None:
            self.probes.append(queued["number"])

        def process_issue(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("probe must not enter normal task processing")

        def revise_issue(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("probe must not enter revision processing")

    service = RoutingService()
    daemon = WorkerDaemon(
        settings,
        QueueGitHub(),
        EventStore(settings.database_path),
        service,
    )

    assert daemon.run_once() is True
    assert service.probes == [7]
