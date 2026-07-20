from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from pathlib import Path
import re
import tomllib
from typing import Any, Iterator, Sequence

from duomac_contracts import ContractError, TaskSpec


_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class RepositoryTarget:
    github: str
    local_path: Path


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


@dataclass(frozen=True, slots=True)
class ActiveTask:
    repo: str
    allowed_paths: tuple[str, ...]


def _bounded_int(raw: dict[str, Any], field: str, minimum: int, maximum: int) -> int:
    value = raw.get(field)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ContractError(
            f"{field} must be an integer from {minimum} to {maximum}"
        )
    return value


def _repository_path(raw: dict[str, Any], index: int) -> Path:
    value = raw.get("local_path")
    field = f"repositories[{index}].local_path"
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    path = Path(value.strip()).expanduser().resolve()
    if not path.is_dir():
        raise ContractError(f"{field} must be an existing directory: {path}")
    return path


def load_scheduled_config(path: Path) -> ScheduledConfig:
    """Load the Mac mini's local Scheduled repository configuration."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ContractError(f"unable to read scheduled config: {exc}") from exc
    if raw.get("schema_version") != 1:
        raise ContractError("scheduled config schema_version must be 1")

    maximum = _bounded_int(raw, "max_parallel_tasks", 1, 8)
    interval = _bounded_int(raw, "poll_interval_minutes", 5, 60)
    entries = raw.get("repositories")
    if not isinstance(entries, list) or not entries:
        raise ContractError("repositories must be a non-empty list")

    repositories: list[RepositoryTarget] = []
    github_names: set[str] = set()
    local_paths: set[Path] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ContractError(f"repositories[{index}] must be a mapping")
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


def _unambiguous_allowed_path(path: object) -> bool:
    if not isinstance(path, str) or not path or path.endswith("/"):
        return False
    if path.startswith("/") or "\\" in path or "\x00" in path:
        return False
    return all(part not in {"", ".", ".."} for part in path.split("/"))


def paths_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Return whether two allowed-path sets cannot safely run in parallel.

    Invalid or absent scopes are treated as conflicts.  Selection must prefer a
    false negative (wait) over a false positive (unsafe parallel execution).
    """
    if not left or not right:
        return True
    if not all(_unambiguous_allowed_path(path) for path in (*left, *right)):
        return True
    return any(
        first == second
        or first.startswith(second + "/")
        or second.startswith(first + "/")
        for first in left
        for second in right
    )


def select_candidate(
    ready: Sequence[Candidate],
    active: Sequence[ActiveTask],
    max_parallel_tasks: int,
) -> Candidate | None:
    """Select the oldest ready candidate that is safe to run locally."""
    if (
        not isinstance(max_parallel_tasks, int)
        or isinstance(max_parallel_tasks, bool)
        or max_parallel_tasks <= 0
        or len(active) >= max_parallel_tasks
    ):
        return None
    for candidate in sorted(ready, key=lambda item: (item.created_at, item.issue_url)):
        conflict = any(
            item.repo.casefold() == candidate.repo.casefold()
            and paths_overlap(item.allowed_paths, candidate.spec.allowed_paths)
            for item in active
        )
        if not conflict:
            return candidate
    return None


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
