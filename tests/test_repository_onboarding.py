from __future__ import annotations

from pathlib import Path

import pytest

from codex_mac_worker.config import parse_project_config
from codex_mac_worker.repository_onboarding import (
    ONBOARDING_PATHS,
    OnboardingError,
    inspect_onboarding_pr,
    load_asset,
    render_project_config,
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
            "mergeable": True,
            "base": {"ref": "main", "sha": "a" * 40},
            "head": {"ref": "codex/onboard-worker", "sha": "b" * 40},
        }

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
