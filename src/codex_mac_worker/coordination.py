from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Iterable

from .protocol import ProtocolError, parse_task_body


TERMINAL_TASK_LABELS = frozenset({"codex:completed", "codex:cancelled"})


def _normalize_repository_path(path: str) -> str:
    if not isinstance(path, str):
        raise ValueError("repository path must be a string")
    raw = path.strip()
    candidate = PurePosixPath(raw)
    if (
        not raw
        or "\\" in raw
        or "\x00" in raw
        or candidate.is_absolute()
        or ".." in candidate.parts
    ):
        raise ValueError(f"invalid repository path: {path}")
    normalized = candidate.as_posix().rstrip("/")
    if normalized in {"", "."}:
        raise ValueError(f"invalid repository path: {path}")
    return normalized


def paths_overlap(left: Iterable[str], right: Iterable[str]) -> bool:
    left_paths = tuple(_normalize_repository_path(path) for path in left)
    right_paths = tuple(_normalize_repository_path(path) for path in right)
    return any(
        left_path == right_path
        or left_path.startswith(right_path + "/")
        or right_path.startswith(left_path + "/")
        for left_path in left_paths
        for right_path in right_paths
    )


def _label_names(issue: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for label in issue.get("labels", []):
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            names.add(name)
    return names


def active_task_conflicts(
    github: Any,
    repo: str,
    allowed_paths: Iterable[str],
    *,
    exclude_issue_number: int | None = None,
    ignore_queued: bool = False,
) -> tuple[str, ...]:
    proposed = tuple(allowed_paths)
    # Validate the proposed scope even when the repository has no active tasks.
    paths_overlap(proposed, ())
    conflicts: list[str] = []
    for issue in github.list_issues(repo, state="open"):
        issue_number = issue.get("number")
        if exclude_issue_number is not None and issue_number == exclude_issue_number:
            continue
        labels = _label_names(issue)
        active_labels = {
            label
            for label in labels
            if label.startswith("codex:") and label not in TERMINAL_TASK_LABELS
        }
        if not active_labels:
            continue
        if ignore_queued and active_labels == {"codex:queued"}:
            continue
        url = str(issue.get("html_url", "")) or f"{repo}#{issue.get('number', '?')}"
        try:
            task = parse_task_body(str(issue.get("body", "")))
            conflict = paths_overlap(proposed, task.allowed_paths)
        except (ProtocolError, ValueError):
            conflict = True
        if conflict:
            conflicts.append(url)
    return tuple(dict.fromkeys(conflicts))
