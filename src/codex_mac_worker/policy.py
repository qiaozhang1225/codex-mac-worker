from __future__ import annotations

from pathlib import PurePosixPath
import re
from typing import Iterable

from .config import ProjectConfig
from .protocol import TaskSpec


class PolicyError(ValueError):
    """Raised when a task or produced diff violates repository policy."""


_FORBIDDEN_OPERATION_PATTERNS = (
    re.compile(r"\bdeploy\b.{0,80}\b(prod|production)\b", re.IGNORECASE),
    re.compile(r"\b(delete|drop|truncate)\b.{0,80}\b(prod|production|database|table|data)\b", re.IGNORECASE),
    re.compile(r"\b(database|schema)\s+migrat(?:e|ion)\b", re.IGNORECASE),
    re.compile(r"(?:部署|上线).{0,20}(?:生产|正式环境)"),
    re.compile(r"(?:删除|清空|迁移).{0,20}(?:生产数据|生产数据库)"),
)


def _normalize(path: str) -> str:
    candidate = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or "\x00" in path
        or candidate.is_absolute()
        or ".." in candidate.parts
    ):
        raise PolicyError(f"invalid repository path: {path}")
    normalized = candidate.as_posix()
    if normalized in {"", "."}:
        raise PolicyError(f"invalid repository path: {path}")
    return normalized.rstrip("/")


def _is_within(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def validate_task_policy(spec: TaskSpec, config: ProjectConfig) -> None:
    if spec.risk not in config.allowed_risk_levels:
        raise PolicyError(f"risk {spec.risk!r} is not allowed")
    if spec.base_branch != config.default_base_branch:
        raise PolicyError("base_branch does not match project policy")
    if spec.verification_profile not in config.verification:
        raise PolicyError(f"unknown verification profile: {spec.verification_profile}")
    requested_work = "\n".join((spec.objective, *spec.acceptance))
    if any(pattern.search(requested_work) for pattern in _FORBIDDEN_OPERATION_PATTERNS):
        raise PolicyError("operational or irreversible objective is not allowed")

    protected = tuple(_normalize(path) for path in config.protected_paths)
    for allowed in spec.allowed_paths:
        allowed_path = _normalize(allowed)
        if any(_is_within(allowed_path, item) or _is_within(item, allowed_path) for item in protected):
            raise PolicyError(f"allowed path overlaps protected path: {allowed}")


def validate_changed_paths(
    spec: TaskSpec,
    config: ProjectConfig,
    changed_paths: Iterable[str],
    diff_lines: int,
) -> None:
    paths = tuple(_normalize(path) for path in changed_paths)
    if len(paths) > config.max_changed_files:
        raise PolicyError("changed-file limit exceeded")
    if diff_lines > config.max_diff_lines:
        raise PolicyError("diff-line limit exceeded")

    protected = tuple(_normalize(path) for path in config.protected_paths)
    allowed = tuple(_normalize(path) for path in spec.allowed_paths)
    for path in paths:
        if any(_is_within(path, item) for item in protected):
            raise PolicyError(f"protected path changed: {path}")
        if not any(_is_within(path, item) for item in allowed):
            raise PolicyError(f"path outside allowed_paths: {path}")
