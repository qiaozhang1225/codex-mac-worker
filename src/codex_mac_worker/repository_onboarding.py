from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib import resources
import json
from pathlib import Path
import re
from typing import Any, Protocol

from .config import parse_project_config


ONBOARDING_PATHS = frozenset(
    {
        ".codex-worker/project.toml",
        ".github/ISSUE_TEMPLATE/codex-task.yml",
        ".github/workflows/codex-worker-watchdog.yml",
    }
)
_ASSETS = {"codex-task.yml", "codex-worker-watchdog.yml"}
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class OnboardingError(RuntimeError):
    """Raised when a repository cannot safely enter the Worker lifecycle."""


class OnboardingGitHub(Protocol):
    def get_pull_request(self, repo: str, pr_number: int) -> dict[str, Any]: ...
    def list_pull_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]: ...
    def get_repository_file(self, repo: str, path: str, *, ref: str) -> str: ...


@dataclass(frozen=True, slots=True)
class OnboardingSnapshot:
    repo: str
    pr_number: int
    url: str
    base_branch: str
    base_sha: str
    head_branch: str
    head_sha: str
    changed_paths: tuple[str, ...]
    project_config_hash: str
    is_draft: bool
    mergeable: bool


def load_asset(name: str) -> str:
    if name not in _ASSETS:
        raise OnboardingError(f"unknown onboarding asset: {name}")
    return (
        resources.files("codex_mac_worker")
        .joinpath("assets", name)
        .read_text(encoding="utf-8")
    )


def inspect_onboarding_pr(
    github: OnboardingGitHub,
    repo: str,
    pr_number: int,
) -> OnboardingSnapshot:
    pull = github.get_pull_request(repo, pr_number)
    files = github.list_pull_files(repo, pr_number)
    paths = {str(item.get("filename", "")) for item in files}
    if paths != ONBOARDING_PATHS or len(files) != len(ONBOARDING_PATHS):
        raise OnboardingError("onboarding PR must contain exactly the three standard files")
    if any(item.get("status") not in {"added", "modified"} for item in files):
        raise OnboardingError("onboarding files cannot be deleted or renamed")

    base = pull.get("base", {})
    head = pull.get("head", {})
    base_branch = str(base.get("ref", ""))
    base_sha = str(base.get("sha", "")).lower()
    head_branch = str(head.get("ref", ""))
    head_sha = str(head.get("sha", "")).lower()
    if not _FULL_SHA_RE.fullmatch(base_sha) or not _FULL_SHA_RE.fullmatch(head_sha):
        raise OnboardingError("onboarding PR must expose full base and head SHAs")

    project_text = github.get_repository_file(
        repo,
        ".codex-worker/project.toml",
        ref=head_sha,
    )
    project = parse_project_config(project_text)
    if project.default_base_branch != base_branch:
        raise OnboardingError("project config default branch does not match PR base")
    for path in ONBOARDING_PATHS - {".codex-worker/project.toml"}:
        content = github.get_repository_file(repo, path, ref=head_sha)
        if content != load_asset(Path(path).name):
            raise OnboardingError(f"standard asset content differs: {path}")

    return OnboardingSnapshot(
        repo=repo,
        pr_number=pr_number,
        url=str(pull.get("html_url", "")),
        base_branch=base_branch,
        base_sha=base_sha,
        head_branch=head_branch,
        head_sha=head_sha,
        changed_paths=tuple(sorted(paths)),
        project_config_hash=hashlib.sha256(project_text.encode("utf-8")).hexdigest(),
        is_draft=bool(pull.get("draft")),
        mergeable=pull.get("mergeable") is True,
    )


def _toml_array(values: tuple[str, ...], *, indent: str = "") -> str:
    lines = ["["]
    lines.extend(f"{indent}  {json.dumps(value, ensure_ascii=False)}," for value in values)
    lines.append(f"{indent}]")
    return "\n".join(lines)


def render_project_config(
    *,
    default_branch: str,
    fast_commands: tuple[str, ...],
    full_commands: tuple[str, ...],
) -> str:
    if not default_branch.strip():
        raise OnboardingError("default branch is required")
    if not fast_commands or any(not item.strip() for item in fast_commands):
        raise OnboardingError("at least one repository-approved fast verification command is required")
    if any(not item.strip() for item in full_commands):
        raise OnboardingError("full verification commands must be non-empty")
    protected = (
        ".codex",
        ".codex-worker",
        ".github/workflows",
        ".env",
        ".env.local",
        "product/deploy",
    )
    parts = [
        "schema_version = 1",
        f"default_base_branch = {json.dumps(default_branch.strip())}",
        'allowed_risk_levels = ["low", "medium"]',
        f"protected_paths = {_toml_array(protected)}",
        "max_changed_files = 30",
        "max_diff_lines = 3000",
        "codex_attempt_timeout_minutes = 45",
        "task_hard_timeout_minutes = 120",
        "max_automatic_attempts = 2",
        "",
        "[verification.fast]",
        f"commands = {_toml_array(fast_commands)}",
    ]
    if full_commands:
        parts.extend(
            [
                "",
                "[verification.full]",
                f"commands = {_toml_array(full_commands)}",
            ]
        )
    rendered = "\n".join(parts) + "\n"
    parse_project_config(rendered)
    return rendered

