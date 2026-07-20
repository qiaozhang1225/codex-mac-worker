from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Any, Iterator, Sequence

from duomac_contracts import (
    ContractError,
    ProjectConfig,
    TaskSpec,
    load_project_config_text,
    parse_issue_body,
    render_issue_body,
    require_current_schema,
    validate_task,
)
from duomac_github import IssueEvent, STATUS_LABELS
from issue_complete import validate_delivery
from issue_checkpoint import validate_payload
from yaml.error import YAMLError


_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_CONFIG_FIELDS = {
    "schema_version",
    "max_parallel_tasks",
    "poll_interval_minutes",
    "repositories",
}
_REPOSITORY_FIELDS = {"github", "local_path"}
_TERMINAL_LABELS = {
    "duomac:blocked",
    "duomac:delivered",
    "duomac:completed",
    "duomac:cancelled",
}
_GITHUB_HTTPS_REMOTE = re.compile(
    r"^https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
_GITHUB_SCP_REMOTE = re.compile(
    r"^git@github\.com:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$"
)
_GITHUB_SSH_REMOTE = re.compile(
    r"^ssh://git@github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


@dataclass(frozen=True, slots=True)
class RepositoryTarget:
    github: str
    local_path: Path


@dataclass(frozen=True, slots=True)
class RepositoryEvidence:
    target: RepositoryTarget
    project: ProjectConfig
    base_commit: str


class RepositoryValidationError(RuntimeError):
    """Raised when local Git evidence does not authorize a Scheduled claim."""


@dataclass(frozen=True, slots=True)
class ScheduledConfig:
    max_parallel_tasks: int
    poll_interval_minutes: int
    repositories: tuple[RepositoryTarget, ...]


@dataclass(frozen=True, slots=True)
class Candidate:
    repo: str
    issue_url: str
    created_at: str
    spec: TaskSpec
    labels: tuple[str, ...] = ()
    events: tuple[IssueEvent, ...] = ()
    state: str = "open"


@dataclass(frozen=True, slots=True)
class ActiveTask:
    repo: str
    allowed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateRejection:
    candidate: object
    reason: str


@dataclass(frozen=True, slots=True)
class SelectionResult:
    candidate: Candidate | None
    reason: str
    skipped: tuple[CandidateRejection, ...]


def _exact_int(raw: dict[str, Any], field: str, expected: int) -> int:
    value = raw.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value != expected:
        raise ContractError(f"{field} must be {expected}")
    return value


def _repository_path(raw: dict[str, Any], index: int) -> Path:
    value = raw.get("local_path")
    field = f"repositories[{index}].local_path"
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    configured = Path(value.strip())
    if not configured.is_absolute():
        raise ContractError(f"{field} must be an absolute path")
    path = configured.resolve()
    if not path.is_dir():
        raise ContractError(f"{field} must be an existing directory: {path}")
    return path


def load_scheduled_config(path: Path) -> ScheduledConfig:
    """Load the Mac mini's local Scheduled repository configuration."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ContractError(f"unable to read scheduled config: {exc}") from exc
    unknown = sorted(set(raw) - _CONFIG_FIELDS)
    if unknown:
        raise ContractError("unknown scheduled config fields: " + ", ".join(unknown))
    if not isinstance(raw.get("schema_version"), int) or isinstance(
        raw.get("schema_version"), bool
    ) or raw.get("schema_version") != 1:
        raise ContractError("scheduled config schema_version must be 1")

    maximum = _exact_int(raw, "max_parallel_tasks", 3)
    interval = _exact_int(raw, "poll_interval_minutes", 10)
    entries = raw.get("repositories")
    if not isinstance(entries, list) or not entries:
        raise ContractError("repositories must be a non-empty list")

    repositories: list[RepositoryTarget] = []
    github_names: set[str] = set()
    local_paths: set[Path] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ContractError(f"repositories[{index}] must be a mapping")
        unknown = sorted(set(entry) - _REPOSITORY_FIELDS)
        if unknown:
            raise ContractError(
                f"unknown repository fields in repositories[{index}]: "
                + ", ".join(unknown)
            )
        github = entry.get("github")
        if not isinstance(github, str) or _REPO.fullmatch(github.strip()) is None:
            raise ContractError(
                f"repositories[{index}].github must be a valid OWNER/REPO"
            )
        github = github.strip()
        normalized_github = github.casefold()
        if normalized_github in github_names:
            raise ContractError("repository GitHub names must be unique")
        local_path = _repository_path(entry, index)
        if local_path in local_paths:
            raise ContractError("repository local paths must be unique")
        github_names.add(normalized_github)
        local_paths.add(local_path)
        repositories.append(RepositoryTarget(github=github, local_path=local_path))

    return ScheduledConfig(
        max_parallel_tasks=maximum,
        poll_interval_minutes=interval,
        repositories=tuple(repositories),
    )


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise RepositoryValidationError("unable to run non-interactive Git") from exc
    if check and result.returncode != 0:
        raise RepositoryValidationError("local Git evidence could not be verified")
    return result


def _git_output(repo: Path, *args: str) -> str:
    return _git(repo, *args).stdout.strip()


def _canonical_github_remote(value: str) -> str | None:
    for pattern in (
        _GITHUB_HTTPS_REMOTE,
        _GITHUB_SCP_REMOTE,
        _GITHUB_SSH_REMOTE,
    ):
        match = pattern.fullmatch(value.strip())
        if match is not None:
            return match.group("repo")
    return None


def validate_repository_target(
    target: RepositoryTarget, spec: TaskSpec
) -> RepositoryEvidence:
    """Validate repository identity and context, then return the fetched base."""
    repo = target.local_path.resolve()
    if not repo.is_dir():
        raise RepositoryValidationError("configured local repository is unavailable")
    top_level = Path(_git_output(repo, "rev-parse", "--show-toplevel")).resolve()
    if top_level != repo:
        raise RepositoryValidationError("configured path is not the Git top level")
    origin = _git_output(repo, "config", "--get", "remote.origin.url")
    canonical = _canonical_github_remote(origin)
    if canonical is None or canonical.casefold() != target.github.casefold():
        raise RepositoryValidationError("configured repository does not match origin")

    if _git(repo, "cat-file", "-e", f"{spec.context_commit}^{{commit}}", check=False).returncode:
        raise RepositoryValidationError("context commit is unavailable")
    project_text = _git_output(
        repo, "show", f"{spec.context_commit}:.duomac/project.toml"
    )
    project = load_project_config_text(project_text)
    for path in spec.context_files:
        if _git(
            repo,
            "cat-file",
            "-e",
            f"{spec.context_commit}:{path}",
            check=False,
        ).returncode:
            raise RepositoryValidationError("a declared context file is unavailable")

    branch = project.default_base_branch
    _git(
        repo,
        "fetch",
        "--no-tags",
        "origin",
        f"refs/heads/{branch}:refs/remotes/origin/{branch}",
    )
    base_commit = _git_output(repo, "rev-parse", f"refs/remotes/origin/{branch}")
    ancestry = _git(
        repo,
        "merge-base",
        "--is-ancestor",
        spec.context_commit,
        base_commit,
        check=False,
    )
    if ancestry.returncode != 0:
        raise RepositoryValidationError(
            "context commit is not an ancestor of the fetched base"
        )
    validate_task(spec, project)
    return RepositoryEvidence(target, project, base_commit)


def _normalized_allowed_path(path: object) -> tuple[str, ...] | None:
    if not isinstance(path, str) or not path or path.endswith("/"):
        return None
    if path.startswith("/") or "\\" in path or "\x00" in path:
        return None
    parts = tuple(component.casefold() for component in path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return parts


def paths_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Return whether two allowed-path sets cannot safely run in parallel.

    Invalid or absent scopes are treated as conflicts.  Selection must prefer a
    false negative (wait) over a false positive (unsafe parallel execution).
    """
    if not left or not right:
        return True
    normalized_left = tuple(_normalized_allowed_path(path) for path in left)
    normalized_right = tuple(_normalized_allowed_path(path) for path in right)
    if None in normalized_left or None in normalized_right:
        return True
    return any(
        first == second
        or first[: len(second)] == second
        or second[: len(first)] == first
        for first in normalized_left
        for second in normalized_right
    )


def _valid_spec(candidate: Candidate) -> str | None:
    spec = candidate.spec
    if not isinstance(spec, TaskSpec):
        return "invalid-spec"
    if spec.schema_version != 2:
        return "schema-version"
    try:
        require_current_schema(parse_issue_body(render_issue_body(spec)))
    except (AttributeError, ContractError, TypeError, ValueError, YAMLError):
        return "invalid-spec"
    return None


def _candidate_rejection_reason(candidate: object) -> str | None:
    if not isinstance(candidate, Candidate):
        return "malformed-candidate"
    if not all(
        isinstance(value, str) and value.strip()
        for value in (candidate.repo, candidate.issue_url, candidate.created_at)
    ):
        return "malformed-candidate"
    spec_reason = _valid_spec(candidate)
    if spec_reason is not None:
        return spec_reason
    if not isinstance(candidate.state, str):
        return "terminal-state"
    if candidate.state.strip().casefold() != "open":
        return "terminal-state"
    if not isinstance(candidate.labels, tuple) or not all(
        isinstance(label, str) and label.strip() for label in candidate.labels
    ):
        return "wrong-dispatch-label"
    labels = {label.strip().casefold() for label in candidate.labels}
    if labels & _TERMINAL_LABELS:
        return "terminal-state"
    if "duomac:ready" not in labels:
        return "wrong-dispatch-label" if labels & set(STATUS_LABELS) else "missing-ready-label"
    if "duomac:active" in labels:
        return "wrong-dispatch-label"
    if not isinstance(candidate.events, tuple):
        return "invalid-events"
    claims = 0
    has_terminal_event = False
    has_unclaimed_progress = False
    for event in candidate.events:
        if not isinstance(event, IssueEvent) or not isinstance(event.payload, dict):
            return "invalid-events"
        payload = event.payload
        event_type = payload.get("type")
        revision = payload.get("revision")
        if (
            not isinstance(event_type, str)
            or event_type not in {"task-start", "checkpoint", "blocked", "delivery"}
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision <= 0
        ):
            return "invalid-events"
        if revision != candidate.spec.revision:
            continue
        if event_type == "task-start":
            try:
                validate_payload(payload)
            except ContractError:
                return "invalid-current-revision-claim"
            claims += 1
        elif event_type == "checkpoint":
            try:
                validate_payload(payload)
            except ContractError:
                return "invalid-events"
            has_unclaimed_progress = True
        elif event_type == "blocked":
            try:
                validate_payload(payload)
            except ContractError:
                return "invalid-events"
            has_terminal_event = True
        else:
            try:
                state = (
                    "completed"
                    if candidate.spec.delivery_mode == "direct-main"
                    else "delivered"
                )
                validate_delivery(payload, candidate.spec, state)
            except ContractError:
                return "invalid-events"
            has_terminal_event = True
    if claims > 1:
        return "invalid-current-revision-claim"
    if has_terminal_event:
        return "terminal-state"
    if claims:
        return "already-claimed"
    if has_unclaimed_progress:
        return "invalid-events"
    return None


def _candidate_sort_key(candidate: object, index: int) -> tuple[str, str, int]:
    if isinstance(candidate, Candidate) and isinstance(candidate.created_at, str) and isinstance(
        candidate.issue_url, str
    ):
        return candidate.created_at, candidate.issue_url, index
    return "", "", index


def _valid_active_tasks(active: object) -> bool:
    if not isinstance(active, Sequence) or isinstance(active, (str, bytes)):
        return False
    for task in active:
        if (
            not isinstance(task, ActiveTask)
            or not isinstance(task.repo, str)
            or _REPO.fullmatch(task.repo) is None
            or not isinstance(task.allowed_paths, tuple)
            or not task.allowed_paths
            or any(_normalized_allowed_path(path) is None for path in task.allowed_paths)
        ):
            return False
    return True


def select_candidate_result(
    ready: Sequence[Candidate],
    active: Sequence[ActiveTask],
    max_parallel_tasks: int,
) -> SelectionResult:
    """Evaluate candidates deterministically without reading or mutating GitHub."""
    if not _valid_active_tasks(active):
        return SelectionResult(None, "invalid-active", ())
    if (
        not isinstance(max_parallel_tasks, int)
        or isinstance(max_parallel_tasks, bool)
        or max_parallel_tasks <= 0
        or len(active) >= max_parallel_tasks
    ):
        return SelectionResult(None, "parallel-limit", ())
    if not ready:
        return SelectionResult(None, "no-ready", ())

    skipped: list[CandidateRejection] = []
    had_path_conflict = False
    for index, candidate in sorted(
        enumerate(ready), key=lambda item: _candidate_sort_key(item[1], item[0])
    ):
        rejection = _candidate_rejection_reason(candidate)
        if rejection is not None:
            skipped.append(CandidateRejection(candidate, rejection))
            continue
        assert isinstance(candidate, Candidate)
        conflict = any(
            item.repo.casefold() == candidate.repo.casefold()
            and paths_overlap(item.allowed_paths, candidate.spec.allowed_paths)
            for item in active
        )
        if conflict:
            had_path_conflict = True
            skipped.append(CandidateRejection(candidate, "path-conflict"))
            continue
        return SelectionResult(candidate, "selected", tuple(skipped))
    reason = "path-conflict" if had_path_conflict else "invalid-candidates-blocked"
    return SelectionResult(None, reason, tuple(skipped))


def select_candidate(
    ready: Sequence[Candidate],
    active: Sequence[ActiveTask],
    max_parallel_tasks: int,
) -> Candidate | None:
    """Select the oldest ready candidate that is safe to run locally."""
    return select_candidate_result(ready, active, max_parallel_tasks).candidate


@contextmanager
def dispatch_lock(path: Path) -> Iterator[None]:
    """Serialize local Scheduled claim attempts without retaining state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
