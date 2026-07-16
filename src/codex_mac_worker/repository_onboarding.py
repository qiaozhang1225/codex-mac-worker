from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Protocol

from .config import parse_project_config
from .control_state import ControlState, operation_id
from .merge_policy import RULESET_NAME, classify_ruleset, ruleset_payload
from .protocol import (
    REPOSITORY_ATTESTATION_MARKER,
    REPOSITORY_PROBE_MARKER,
    ProtocolError,
    parse_repository_attestation,
    parse_repository_probe,
    render_repository_probe,
)
from .references import PullRequestReference


ONBOARDING_PATHS = frozenset(
    {
        ".codex-worker/project.toml",
        ".github/ISSUE_TEMPLATE/codex-task.yml",
        ".github/workflows/codex-worker-watchdog.yml",
    }
)
_ASSETS = {"codex-task.yml", "codex-worker-watchdog.yml"}
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
STATUS_LABELS: dict[str, tuple[str, str]] = {
    "codex:queued": ("1f6feb", "Ready for the Mac mini Worker"),
    "codex:claimed": ("8250df", "Claimed or paused by the Worker"),
    "codex:running": ("0969da", "Codex execution is active"),
    "codex:verifying": ("bf8700", "Repository-approved checks are running"),
    "codex:retrying": ("d4a72c", "One bounded automatic retry is active"),
    "codex:awaiting-review": ("2da44e", "Draft PR awaits human review"),
    "codex:merging": ("8957e5", "Verified Worker PR is being auto-merged"),
    "codex:needs-attention": ("cf222e", "Human intervention is required"),
    "codex:completed": ("0e8a16", "Worker delivery was merged"),
    "codex:cancelled": ("6e7781", "Task was cancelled"),
}


class OnboardingError(RuntimeError):
    """Raised when a repository cannot safely enter the Worker lifecycle."""


class OnboardingGitHub(Protocol):
    def get_repository(self, repo: str) -> dict[str, Any]: ...
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


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    repo: str
    phase: str
    default_branch: str
    default_head: str
    files_valid: bool
    labels_valid: bool
    ruleset_valid: bool
    ruleset_profile: str | None
    worker_attested: bool
    worker_login: str | None
    blockers: tuple[str, ...]


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
    repository = github.get_repository(repo)
    files = github.list_pull_files(repo, pr_number)
    paths = {str(item.get("filename", "")) for item in files}
    if paths != ONBOARDING_PATHS or len(files) != len(ONBOARDING_PATHS):
        raise OnboardingError("onboarding PR must contain exactly the three standard files")
    if any(item.get("status") not in {"added", "modified"} for item in files):
        raise OnboardingError("onboarding files cannot be deleted or renamed")

    base = pull.get("base", {})
    head = pull.get("head", {})
    default_branch = str(repository.get("default_branch", ""))
    if pull.get("state") != "open" and not pull.get("merged_at"):
        raise OnboardingError("onboarding PR must be open")
    if str(base.get("ref", "")) != default_branch:
        raise OnboardingError("onboarding PR must target the current default branch")
    if str(base.get("repo", {}).get("full_name", "")) != repo:
        raise OnboardingError("onboarding PR base must be the target repository")
    if str(head.get("repo", {}).get("full_name", "")) != repo:
        raise OnboardingError("onboarding PR head must be in the same repository")
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


def _run_git(
    cwd: Path,
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=merged_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise OnboardingError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


@contextmanager
def _git_authentication(root: Path, token: str):
    askpass = root / "git-askpass.sh"
    askpass.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *Username*) printf '%s' 'x-access-token' ;;\n"
        "  *) printf '%s' \"$CODEXCTL_GIT_TOKEN\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    askpass.chmod(0o700)
    try:
        yield {
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
            "CODEXCTL_GIT_TOKEN": token,
        }
    finally:
        askpass.unlink(missing_ok=True)


def prepare_onboarding(
    github: Any,
    repo: str,
    *,
    adopt_pr: int | None = None,
    project_config_path: Path | None = None,
    token: str,
) -> OnboardingSnapshot:
    if adopt_pr is not None:
        return inspect_onboarding_pr(github, repo, adopt_pr)
    if project_config_path is None:
        raise OnboardingError("--project-config is required when creating onboarding")

    repository = github.get_repository(repo)
    default_branch = str(repository.get("default_branch", ""))
    clone_url = str(repository.get("clone_url", ""))
    if not default_branch or not clone_url:
        raise OnboardingError("repository clone URL or default branch is missing")
    project_text = project_config_path.read_text(encoding="utf-8")
    project = parse_project_config(project_text)
    if project.default_base_branch != default_branch:
        raise OnboardingError("project config default branch does not match repository")

    branch = "codex/onboard-worker"
    existing = github.find_open_pull_request(repo, branch)
    if existing is not None:
        return inspect_onboarding_pr(github, repo, int(existing["number"]))

    with tempfile.TemporaryDirectory(prefix="codexctl-onboard-") as raw_root:
        root = Path(raw_root)
        checkout = root / "repository"
        with _git_authentication(root, token) as auth_env:
            _run_git(
                root,
                "clone",
                "--branch",
                default_branch,
                "--single-branch",
                clone_url,
                str(checkout),
                env=auth_env,
            )
            remote_branch = _run_git(
                checkout,
                "ls-remote",
                "--exit-code",
                "--heads",
                "origin",
                f"refs/heads/{branch}",
                env=auth_env,
                check=False,
            )
            if remote_branch.returncode == 0:
                _run_git(
                    checkout,
                    "fetch",
                    "origin",
                    f"refs/heads/{branch}:refs/remotes/origin/{branch}",
                    env=auth_env,
                )
                _run_git(checkout, "switch", "-c", branch, f"refs/remotes/origin/{branch}")
                changed = set(
                    _run_git(
                        checkout,
                        "diff",
                        "--name-only",
                        f"{default_branch}...{branch}",
                    ).stdout.splitlines()
                )
                if changed != ONBOARDING_PATHS:
                    raise OnboardingError(
                        f"remote branch {branch} does not contain the exact onboarding paths"
                    )
                for path in ONBOARDING_PATHS:
                    expected = (
                        project_text
                        if path == ".codex-worker/project.toml"
                        else load_asset(Path(path).name)
                    )
                    if (checkout / path).read_text(encoding="utf-8") != expected:
                        raise OnboardingError(
                            f"remote branch {branch} onboarding content differs: {path}"
                        )
            else:
                _run_git(checkout, "switch", "-c", branch)
                for path in ONBOARDING_PATHS:
                    target = checkout / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if path == ".codex-worker/project.toml":
                        target.write_text(project_text, encoding="utf-8")
                    else:
                        target.write_text(load_asset(Path(path).name), encoding="utf-8")
                _run_git(checkout, "add", *sorted(ONBOARDING_PATHS))
                staged = set(
                    _run_git(checkout, "diff", "--cached", "--name-only").stdout.splitlines()
                )
                if staged != ONBOARDING_PATHS:
                    raise OnboardingError(
                        "onboarding commit does not contain the exact standard paths"
                    )
                user = github.get_authenticated_user()
                login = str(user.get("login", "codexctl"))
                user_id = str(user.get("id", ""))
                commit_env = {
                    "GIT_AUTHOR_NAME": login,
                    "GIT_COMMITTER_NAME": login,
                    "GIT_AUTHOR_EMAIL": f"{user_id}+{login}@users.noreply.github.com",
                    "GIT_COMMITTER_EMAIL": f"{user_id}+{login}@users.noreply.github.com",
                }
                _run_git(
                    checkout,
                    "commit",
                    "-m",
                    "chore: onboard repository to Codex Worker",
                    env=commit_env,
                )
                _run_git(
                    checkout,
                    "push",
                    "origin",
                    f"HEAD:refs/heads/{branch}",
                    env=auth_env,
                )

    pull = github.create_draft_pr(
        repo,
        branch,
        default_branch,
        "chore: onboard repository to Mac Codex Worker",
        "<!-- codex-repository-onboarding:v1 -->\n"
        "Adds only the repository-scoped Worker policy, fallback Issue form, and watchdog.\n",
    )
    return inspect_onboarding_pr(github, repo, int(pull["number"]))


def _toml_array(values: tuple[str, ...], *, indent: str = "") -> str:
    lines = ["["]
    lines.extend(f"{indent}  {json.dumps(value, ensure_ascii=False)}," for value in values)
    lines.append(f"{indent}]")
    return "\n".join(lines)


def render_project_config(
    *,
    default_branch: str,
    worker_github_app_id: int,
    fast_commands: tuple[str, ...],
    full_commands: tuple[str, ...],
) -> str:
    if not default_branch.strip():
        raise OnboardingError("default branch is required")
    if (
        not isinstance(worker_github_app_id, int)
        or isinstance(worker_github_app_id, bool)
        or worker_github_app_id <= 0
    ):
        raise OnboardingError("Worker GitHub App ID must be a positive integer")
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
        "schema_version = 2",
        f"default_base_branch = {json.dumps(default_branch.strip())}",
        f"worker_github_app_id = {worker_github_app_id}",
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


def _ruleset_valid(payload: dict[str, Any]) -> bool:
    return classify_ruleset(payload) is not None


def _default_repository_state(
    github: Any,
    repo: str,
) -> tuple[str, str, str, int | None, bool, list[str]]:
    blockers: list[str] = []
    try:
        repository = github.get_repository(repo)
        default_branch = str(repository.get("default_branch", ""))
        commit = github.get_commit(repo, default_branch)
        default_head = str(commit.get("sha", "")).lower()
        if not default_branch or not _FULL_SHA_RE.fullmatch(default_head):
            raise OnboardingError("repository default branch metadata is incomplete")
        project_text = github.get_repository_file(
            repo,
            ".codex-worker/project.toml",
            ref=default_head,
        )
        project = parse_project_config(project_text)
        if project.default_base_branch != default_branch:
            raise OnboardingError("project config default branch does not match repository")
        for path in ONBOARDING_PATHS - {".codex-worker/project.toml"}:
            content = github.get_repository_file(repo, path, ref=default_head)
            if content != load_asset(Path(path).name):
                raise OnboardingError(f"standard asset content differs: {path}")
        project_hash = hashlib.sha256(project_text.encode("utf-8")).hexdigest()
        return (
            default_branch,
            default_head,
            project_hash,
            project.worker_github_app_id,
            True,
            blockers,
        )
    except Exception as exc:
        blockers.append(f"onboarding files: {type(exc).__name__}: {exc}")
        return "", "", "", None, False, blockers


def repository_status(github: Any, repo: str) -> ReadinessReport:
    (
        default_branch,
        default_head,
        project_hash,
        worker_github_app_id,
        files_valid,
        blockers,
    ) = _default_repository_state(github, repo)

    labels_valid = False
    try:
        labels = {str(item.get("name")): item for item in github.list_labels(repo)}
        labels_valid = all(
            name in labels
            and str(labels[name].get("color", "")).lower() == color
            and str(labels[name].get("description", "")) == description
            for name, (color, description) in STATUS_LABELS.items()
        )
    except Exception as exc:
        blockers.append(f"labels: {type(exc).__name__}: {exc}")
    if not labels_valid:
        blockers.append("labels: standard codex status labels are missing or changed")

    ruleset_profile: str | None = None
    try:
        summaries = github.list_rulesets(repo)
        matching = [item for item in summaries if item.get("name") == RULESET_NAME]
        if len(matching) == 1:
            ruleset = github.get_ruleset(repo, int(matching[0]["id"]))
            ruleset_profile = classify_ruleset(ruleset)
    except Exception as exc:
        blockers.append(f"Ruleset: {type(exc).__name__}: {exc}")
    ruleset_valid = ruleset_profile is not None
    if not ruleset_valid:
        blockers.append("Ruleset: Codex Worker default-branch protection is missing or unsafe")

    worker_attested = False
    worker_login: str | None = None
    if files_valid:
        try:
            for issue in reversed(github.list_issues(repo, state="all")):
                body = str(issue.get("body", ""))
                if REPOSITORY_PROBE_MARKER not in body:
                    continue
                try:
                    probe = parse_repository_probe(body)
                except ProtocolError:
                    continue
                if (
                    probe.project_config_hash != project_hash
                ):
                    continue
                for comment in reversed(
                    github.list_comments(repo, int(issue["number"]))
                ):
                    if REPOSITORY_ATTESTATION_MARKER not in str(comment.get("body", "")):
                        continue
                    user = comment.get("user", {})
                    if user.get("type") != "Bot":
                        continue
                    app_metadata = comment.get("performed_via_github_app")
                    app_id = (
                        app_metadata.get("id")
                        if isinstance(app_metadata, dict)
                        else None
                    )
                    if app_id != worker_github_app_id:
                        continue
                    try:
                        attestation = parse_repository_attestation(str(comment["body"]))
                    except ProtocolError:
                        continue
                    if (
                        attestation.probe_id == probe.probe_id
                        and attestation.default_head == probe.default_head
                        and attestation.project_config_hash == project_hash
                    ):
                        worker_attested = True
                        worker_login = str(user.get("login", "")) or None
                        break
                if worker_attested:
                    break
        except Exception as exc:
            blockers.append(f"worker attestation: {type(exc).__name__}: {exc}")

    if not files_valid:
        phase = "unconfigured"
    elif not labels_valid or not ruleset_valid:
        phase = "blocked"
    elif worker_attested:
        phase = "ready"
    else:
        phase = "awaiting-worker"
    if not worker_attested and files_valid:
        blockers.append("worker attestation: matching Mac mini readiness proof is pending")
    return ReadinessReport(
        repo=repo,
        phase=phase,
        default_branch=default_branch,
        default_head=default_head,
        files_valid=files_valid,
        labels_valid=labels_valid,
        ruleset_valid=ruleset_valid,
        ruleset_profile=ruleset_profile,
        worker_attested=worker_attested,
        worker_login=worker_login,
        blockers=tuple(dict.fromkeys(blockers)),
    )


def _reconcile_ruleset(github: Any, repo: str) -> None:
    expected = ruleset_payload()
    matching = [
        item for item in github.list_rulesets(repo) if item.get("name") == RULESET_NAME
    ]
    if len(matching) > 1:
        raise OnboardingError("multiple Codex Worker Rulesets exist")
    if not matching:
        github.create_ruleset(repo, expected)
        return
    ruleset_id = int(matching[0]["id"])
    current = github.get_ruleset(repo, ruleset_id)
    if any(
        item.get("actor_type") == "Integration"
        for item in current.get("bypass_actors", [])
    ):
        raise OnboardingError("existing Ruleset grants an Integration bypass")
    if classify_ruleset(current) is None:
        github.update_ruleset(repo, ruleset_id, expected)


def _ensure_probe(
    github: Any,
    repo: str,
    *,
    default_head: str,
    project_config_hash: str,
    probe_id: str,
) -> None:
    for issue in github.list_issues(repo, state="all"):
        body = str(issue.get("body", ""))
        if REPOSITORY_PROBE_MARKER not in body:
            continue
        try:
            probe = parse_repository_probe(body)
        except ProtocolError:
            continue
        if probe.probe_id == probe_id:
            return
    github.create_issue(
        repo,
        "[Codex] Repository readiness probe",
        render_repository_probe(
            probe_id=probe_id,
            default_head=default_head,
            project_config_hash=project_config_hash,
        ),
        ["codex:queued"],
    )


def _validate_onboarding_review_gates(
    github: Any,
    snapshot: OnboardingSnapshot,
) -> None:
    for check in github.list_check_runs(snapshot.repo, snapshot.head_sha):
        status = str(check.get("status") or "")
        conclusion = str(check.get("conclusion") or "")
        if status != "completed" or conclusion not in {"success", "neutral"}:
            raise OnboardingError(
                f"onboarding check {check.get('name', 'unnamed')} is not successful"
            )
    combined = github.get_combined_status(snapshot.repo, snapshot.head_sha)
    for status in combined.get("statuses", []):
        if status.get("state") != "success":
            raise OnboardingError(
                f"onboarding legacy check {status.get('context', 'unnamed')} is not successful"
            )
    unresolved = [
        thread
        for thread in github.list_review_threads(snapshot.repo, snapshot.pr_number)
        if thread.get("isResolved") is not True
    ]
    if unresolved:
        raise OnboardingError(
            f"onboarding PR has {len(unresolved)} unresolved review threads"
        )


def finalize_onboarding(
    github: Any,
    state: ControlState,
    reference: PullRequestReference,
    *,
    expected_head: str,
) -> ReadinessReport:
    if not _FULL_SHA_RE.fullmatch(expected_head):
        raise OnboardingError("expected head must be a full 40-character SHA")
    snapshot = inspect_onboarding_pr(github, reference.repo, reference.number)
    if snapshot.head_sha != expected_head.lower():
        raise OnboardingError("approval expired because the onboarding PR head changed")
    pull = github.get_pull_request(reference.repo, reference.number)
    key = operation_id(
        "repo-finalize",
        f"{reference.repo}#{reference.number}",
        expected_head.lower(),
    )
    existing_operation = state.get(key)
    if existing_operation and existing_operation.get("state") == "completed":
        return repository_status(github, reference.repo)
    _validate_onboarding_review_gates(github, snapshot)
    state.begin(
        key,
        "repo-finalize",
        f"{reference.repo}#{reference.number}",
        expected_head.lower(),
    )

    if not pull.get("merged_at"):
        if not snapshot.mergeable:
            raise OnboardingError("onboarding PR is not cleanly mergeable")
        if snapshot.is_draft:
            github.mark_pull_request_ready(reference.repo, reference.number)
            snapshot = inspect_onboarding_pr(github, reference.repo, reference.number)
            if snapshot.head_sha != expected_head.lower():
                raise OnboardingError("approval expired while marking the PR ready")
        _validate_onboarding_review_gates(github, snapshot)
        try:
            result = github.merge_pull_request(
                reference.repo,
                reference.number,
                expected_head=expected_head.lower(),
            )
            if not result.get("merged"):
                raise OnboardingError(f"GitHub refused onboarding merge: {result}")
        except Exception:
            reconciled = github.get_pull_request(reference.repo, reference.number)
            if not reconciled.get("merged_at"):
                raise
            reconciled_head = str(
                reconciled.get("head", {}).get("sha", "")
            ).lower()
            if reconciled_head != expected_head.lower():
                raise OnboardingError(
                    "onboarding merge completed with an unexpected head"
                )

    default_branch, default_head, project_hash, _, files_valid, blockers = (
        _default_repository_state(github, reference.repo)
    )
    if not files_valid:
        raise OnboardingError("merged onboarding files are invalid: " + "; ".join(blockers))
    for name, (color, description) in STATUS_LABELS.items():
        github.upsert_label(reference.repo, name, color, description)
    _reconcile_ruleset(github, reference.repo)
    _ensure_probe(
        github,
        reference.repo,
        default_head=default_head,
        project_config_hash=project_hash,
        probe_id=f"repo-finalize-{key[:24]}",
    )
    state.complete(
        key,
        {
            "merged": True,
            "approved_head": expected_head.lower(),
            "default_branch": default_branch,
            "default_head": default_head,
        },
    )
    return repository_status(github, reference.repo)
