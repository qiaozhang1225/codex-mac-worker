from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

import pytest

from codex_mac_worker.config import parse_project_config
from codex_mac_worker.control_state import ControlState
from codex_mac_worker.protocol import render_repository_attestation, render_repository_probe
from codex_mac_worker.references import PullRequestReference
from codex_mac_worker.repository_onboarding import (
    ONBOARDING_PATHS,
    STATUS_LABELS,
    OnboardingError,
    finalize_onboarding,
    inspect_onboarding_pr,
    load_asset,
    prepare_onboarding,
    repository_status,
    render_project_config,
    ruleset_payload,
)


PROJECT_TOML = """
schema_version = 1
default_base_branch = "main"
allowed_risk_levels = ["low", "medium"]
protected_paths = [".codex", ".codex-worker", ".github/workflows", ".env"]
max_changed_files = 30
max_diff_lines = 3000
codex_attempt_timeout_minutes = 45
task_hard_timeout_minutes = 120
max_automatic_attempts = 2
[verification.fast]
commands = ["python -m unittest"]
""".strip() + "\n"


class OnboardingGitHub:
    def __init__(self, *, extra_file: str | None = None, asset_drift: bool = False) -> None:
        self.extra_file = extra_file
        self.asset_drift = asset_drift

    def get_pull_request(self, repo: str, pr_number: int) -> dict:
        return {
            "number": pr_number,
            "html_url": f"https://github.com/{repo}/pull/{pr_number}",
            "draft": True,
            "state": "open",
            "mergeable": True,
            "base": {"ref": "main", "sha": "a" * 40, "repo": {"full_name": repo}},
            "head": {
                "ref": "codex/onboard-worker",
                "sha": "b" * 40,
                "repo": {"full_name": repo},
            },
        }

    def get_repository(self, repo: str) -> dict:
        return {"default_branch": "main"}

    def list_pull_files(self, repo: str, pr_number: int) -> list[dict]:
        files = [{"filename": path, "status": "added"} for path in sorted(ONBOARDING_PATHS)]
        if self.extra_file:
            files.append({"filename": self.extra_file, "status": "added"})
        return files

    def get_repository_file(self, repo: str, path: str, *, ref: str) -> str:
        assert ref == "b" * 40
        if path == ".codex-worker/project.toml":
            return PROJECT_TOML
        asset = load_asset(Path(path).name)
        return asset + "# drift\n" if self.asset_drift else asset


def test_packaged_assets_contain_required_machine_contracts() -> None:
    assert "<!-- codex-task:v1 -->" in load_asset("codex-task.yml")
    assert "codex-worker-status:v1" in load_asset("codex-worker-watchdog.yml")


def test_onboarding_snapshot_accepts_only_three_standard_files() -> None:
    snapshot = inspect_onboarding_pr(OnboardingGitHub(), "owner/repo", 1)

    assert snapshot.changed_paths == tuple(sorted(ONBOARDING_PATHS))
    assert snapshot.head_sha == "b" * 40
    assert snapshot.base_sha == "a" * 40
    assert len(snapshot.project_config_hash) == 64


def test_onboarding_snapshot_rejects_a_fourth_file() -> None:
    with pytest.raises(OnboardingError, match="exactly the three standard files"):
        inspect_onboarding_pr(OnboardingGitHub(extra_file="README.md"), "owner/repo", 1)


def test_onboarding_snapshot_rejects_standard_asset_drift() -> None:
    with pytest.raises(OnboardingError, match="standard asset"):
        inspect_onboarding_pr(OnboardingGitHub(asset_drift=True), "owner/repo", 1)


def test_onboarding_snapshot_rejects_foreign_head_repository() -> None:
    class ForeignHeadGitHub(OnboardingGitHub):
        def get_pull_request(self, repo: str, pr_number: int) -> dict:
            pull = super().get_pull_request(repo, pr_number)
            pull["head"]["repo"]["full_name"] = "attacker/fork"
            return pull

    with pytest.raises(OnboardingError, match="same repository"):
        inspect_onboarding_pr(ForeignHeadGitHub(), "owner/repo", 1)


def test_project_config_renderer_requires_explicit_fast_commands() -> None:
    with pytest.raises(OnboardingError, match="fast verification"):
        render_project_config(default_branch="main", fast_commands=(), full_commands=())

    rendered = render_project_config(
        default_branch="main",
        fast_commands=("python -m unittest",),
        full_commands=("python -m unittest", "npm run build"),
    )
    config = parse_project_config(rendered)

    assert config.verification["fast"] == ("python -m unittest",)
    assert config.verification["full"] == ("python -m unittest", "npm run build")


class FinalizeGitHub(OnboardingGitHub):
    def __init__(self, *, head_sha: str = "b" * 40) -> None:
        super().__init__()
        self.head_sha = head_sha
        self.draft = True
        self.merged = False
        self.writes: list[str] = []
        self.labels: dict[str, dict] = {}
        self.rulesets: list[dict] = []
        self.issues: list[dict] = []
        self.comments: dict[int, list[dict]] = {}
        self.default_head = "c" * 40

    def get_pull_request(self, repo: str, pr_number: int) -> dict:
        payload = super().get_pull_request(repo, pr_number)
        payload["draft"] = self.draft
        payload["head"]["sha"] = self.head_sha
        payload["merged_at"] = "2026-07-14T10:00:00Z" if self.merged else None
        return payload

    def get_repository_file(self, repo: str, path: str, *, ref: str) -> str:
        if path == ".codex-worker/project.toml":
            return PROJECT_TOML
        return load_asset(Path(path).name)

    def mark_pull_request_ready(self, repo: str, pr_number: int) -> dict:
        self.writes.append("ready")
        self.draft = False
        return {"number": pr_number, "isDraft": False}

    def merge_pull_request(self, repo: str, pr_number: int, *, expected_head: str) -> dict:
        self.writes.append("merge")
        assert expected_head == self.head_sha
        self.merged = True
        return {"merged": True, "sha": "c" * 40}

    def get_repository(self, repo: str) -> dict:
        return {"full_name": repo, "default_branch": "main"}

    def get_commit(self, repo: str, ref: str) -> dict:
        return {"sha": self.default_head}

    def list_labels(self, repo: str) -> list[dict]:
        return list(self.labels.values())

    def upsert_label(self, repo: str, name: str, color: str, description: str) -> dict:
        self.writes.append(f"label:{name}")
        payload = {"name": name, "color": color, "description": description}
        self.labels[name] = payload
        return payload

    def list_rulesets(self, repo: str) -> list[dict]:
        return [{"id": item["id"], "name": item["name"]} for item in self.rulesets]

    def get_ruleset(self, repo: str, ruleset_id: int) -> dict:
        return next(item for item in self.rulesets if item["id"] == ruleset_id)

    def create_ruleset(self, repo: str, payload: dict) -> dict:
        self.writes.append("ruleset:create")
        created = {"id": 7, **payload}
        self.rulesets.append(created)
        return created

    def update_ruleset(self, repo: str, ruleset_id: int, payload: dict) -> dict:
        self.writes.append("ruleset:update")
        updated = {"id": ruleset_id, **payload}
        self.rulesets = [updated if item["id"] == ruleset_id else item for item in self.rulesets]
        return updated

    def list_issues(self, repo: str, *, state: str = "open", labels: str | None = None) -> list[dict]:
        return self.issues

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> dict:
        self.writes.append("probe:create")
        issue = {
            "number": len(self.issues) + 10,
            "title": title,
            "body": body,
            "labels": [{"name": item} for item in labels],
            "user": {"login": "owner", "type": "User"},
        }
        self.issues.append(issue)
        return issue

    def list_comments(self, repo: str, issue_number: int) -> list[dict]:
        return self.comments.get(issue_number, [])


def test_finalize_rejects_head_drift_before_any_write(tmp_path: Path) -> None:
    github = FinalizeGitHub(head_sha="d" * 40)

    with pytest.raises(OnboardingError, match="approval expired"):
        finalize_onboarding(
            github,
            ControlState(tmp_path / "state.db"),
            PullRequestReference("owner/repo", 1),
            expected_head="b" * 40,
        )

    assert github.writes == []


def test_finalize_merges_reconciles_policy_and_creates_one_probe(tmp_path: Path) -> None:
    github = FinalizeGitHub()
    state = ControlState(tmp_path / "state.db")

    report = finalize_onboarding(
        github,
        state,
        PullRequestReference("owner/repo", 1),
        expected_head="b" * 40,
    )
    repeated = finalize_onboarding(
        github,
        state,
        PullRequestReference("owner/repo", 1),
        expected_head="b" * 40,
    )

    assert report.phase == "awaiting-worker"
    assert repeated.phase == "awaiting-worker"
    assert github.writes.count("merge") == 1
    assert github.writes.count("ready") == 1
    assert sum(item.startswith("label:") for item in github.writes) == len(STATUS_LABELS)
    assert github.writes.count("ruleset:create") == 1
    assert len(github.labels) == len(STATUS_LABELS)
    assert len(github.rulesets) == 1
    assert len(github.issues) == 1


def test_finalize_rejects_uncertain_merge_with_changed_head(tmp_path: Path) -> None:
    class UncertainGitHub(FinalizeGitHub):
        def merge_pull_request(
            self, repo: str, pr_number: int, *, expected_head: str
        ) -> dict:
            self.writes.append("merge")
            self.merged = True
            self.head_sha = "d" * 40
            raise RuntimeError("connection dropped")

    github = UncertainGitHub()
    with pytest.raises(OnboardingError, match="head"):
        finalize_onboarding(
            github,
            ControlState(tmp_path / "state.db"),
            PullRequestReference("owner/repo", 1),
            expected_head="b" * 40,
        )


def test_repository_status_becomes_ready_only_for_matching_bot_attestation() -> None:
    github = FinalizeGitHub()
    github.merged = True
    for name, (color, description) in STATUS_LABELS.items():
        github.labels[name] = {"name": name, "color": color, "description": description}
    github.rulesets = [{"id": 7, **ruleset_payload()}]
    probe = github.create_issue(
        "owner/repo",
        "[Codex] Repository readiness probe",
        render_repository_probe(
            probe_id="probe-1",
            default_head="c" * 40,
            project_config_hash=hashlib.sha256(PROJECT_TOML.encode()).hexdigest(),
        ),
        ["codex:queued"],
    )
    github.comments[probe["number"]] = [
        {
            "body": render_repository_attestation(
                probe_id="probe-1",
                worker_id="mac-mini",
                default_head="c" * 40,
                project_config_hash=hashlib.sha256(PROJECT_TOML.encode()).hexdigest(),
                attested_at="2026-07-14T10:00:00+00:00",
            ),
            "user": {"login": "coworker-app[bot]", "type": "Bot"},
        }
    ]

    report = repository_status(github, "owner/repo")

    assert report.phase == "ready"
    assert report.worker_attested is True
    assert report.worker_login == "coworker-app[bot]"

    github.default_head = "d" * 40
    after_product_merge = repository_status(github, "owner/repo")
    assert after_product_merge.phase == "ready"
    assert after_product_merge.worker_login == "coworker-app[bot]"

    github.rulesets[0]["bypass_actors"].append(
        {"actor_id": 123, "actor_type": "User", "bypass_mode": "always"}
    )
    unsafe_bypass = repository_status(github, "owner/repo")
    assert unsafe_bypass.phase == "blocked"
    assert unsafe_bypass.ruleset_valid is False


def test_prepare_onboarding_creates_and_pushes_exact_standard_branch(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=source, check=True)
    (source / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=source, check=True, capture_output=True)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(source), str(remote)], check=True, capture_output=True)
    project = tmp_path / "project.toml"
    project.write_text(PROJECT_TOML, encoding="utf-8")

    class LocalGitHub:
        def get_repository(self, repo: str) -> dict:
            return {"default_branch": "main", "clone_url": str(remote)}

        def get_authenticated_user(self) -> dict:
            return {"login": "owner", "id": 123}

        def find_open_pull_request(self, repo: str, branch: str) -> None:
            return None

        def create_draft_pr(
            self, repo: str, head: str, base: str, title: str, body: str
        ) -> dict:
            return {"number": 1, "html_url": "https://github.com/owner/repo/pull/1"}

        def _git(self, *args: str) -> str:
            return subprocess.run(
                ["git", *args], cwd=remote, check=True, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ).stdout.strip()

        def get_pull_request(self, repo: str, pr_number: int) -> dict:
            return {
                "number": 1,
                "html_url": "https://github.com/owner/repo/pull/1",
                "draft": True,
                "state": "open",
                "mergeable": True,
                "base": {
                    "ref": "main",
                    "sha": self._git("rev-parse", "main"),
                    "repo": {"full_name": repo},
                },
                "head": {
                    "ref": "codex/onboard-worker",
                    "sha": self._git("rev-parse", "codex/onboard-worker"),
                    "repo": {"full_name": repo},
                },
            }

        def list_pull_files(self, repo: str, pr_number: int) -> list[dict]:
            output = self._git("diff", "--name-status", "main..codex/onboard-worker")
            status_map = {"A": "added", "M": "modified"}
            return [
                {"filename": line.split("\t", 1)[1], "status": status_map[line[0]]}
                for line in output.splitlines()
            ]

        def get_repository_file(self, repo: str, path: str, *, ref: str) -> str:
            return subprocess.run(
                ["git", "show", f"{ref}:{path}"],
                cwd=remote,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout

    snapshot = prepare_onboarding(
        LocalGitHub(),
        "owner/repo",
        project_config_path=project,
        token="not-used-by-local-remote",
    )

    assert snapshot.changed_paths == tuple(sorted(ONBOARDING_PATHS))
    assert snapshot.head_branch == "codex/onboard-worker"

    recovered = prepare_onboarding(
        LocalGitHub(),
        "owner/repo",
        project_config_path=project,
        token="not-used-by-local-remote",
    )
    assert recovered.head_sha == snapshot.head_sha
