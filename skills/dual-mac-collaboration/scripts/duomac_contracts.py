from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import tomllib
from typing import Any, Literal

import yaml


TASK_MARKER = "<!-- duomac-task:v1 -->"
_TASK_BLOCK = re.compile(
    re.escape(TASK_MARKER) + r"\s*```yaml\s*\n(?P<yaml>.*?)\n```",
    re.DOTALL,
)
_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_FORBIDDEN_OPERATIONS = (
    re.compile(r"\bdeploy\b.{0,80}\b(prod|production)\b", re.IGNORECASE),
    re.compile(
        r"\b(delete|drop|truncate)\b.{0,80}\b(prod|production|database|table|data)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(database|schema)\s+migrat(?:e|ion)\b", re.IGNORECASE),
    re.compile(r"(?:部署|上线).{0,20}(?:生产|正式环境)"),
    re.compile(r"(?:删除|清空|迁移).{0,20}(?:生产数据|生产数据库)"),
)


class ContractError(ValueError):
    """Raised when a project config or Issue task contract is invalid."""


@dataclass(frozen=True, slots=True)
class Milestone:
    number: int
    objective: str
    steps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskSpec:
    schema_version: Literal[1, 2]
    revision: int
    dispatcher: str
    executor: str
    objective: str
    context_commit: str
    context_files: tuple[str, ...]
    decisions: tuple[str, ...]
    acceptance: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    execution_plan: tuple[Milestone, ...]
    verification_profile: str
    delivery_mode: Literal["direct-main", "task-branch"]
    risk: Literal["low", "medium"]


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    default_base_branch: str
    protected_paths: tuple[str, ...]
    max_changed_files: int
    max_diff_lines: int
    verification: dict[str, tuple[str, ...]]


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{field} must be a mapping")
    return value


def _string(mapping: dict[str, Any], field: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    return value.strip()


def _positive_int(mapping: dict[str, Any], field: str) -> int:
    value = mapping.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ContractError(f"{field} must be a positive integer")
    return value


def _strings(mapping: dict[str, Any], field: str) -> tuple[str, ...]:
    value = mapping.get(field)
    if not isinstance(value, list) or not value:
        raise ContractError(f"{field} must be a non-empty string list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ContractError(f"{field} must be a non-empty string list")
    return tuple(item.strip() for item in value)


def _milestones(task: dict[str, Any], schema_version: int) -> tuple[Milestone, ...]:
    raw = task.get("execution_plan")
    if not isinstance(raw, list) or not raw:
        raise ContractError("execution_plan must be a non-empty list")
    if schema_version == 1:
        if not all(isinstance(item, str) and item.strip() for item in raw):
            raise ContractError("schema v1 execution_plan must be a string list")
        return tuple(
            Milestone(index, item.strip(), (item.strip(),))
            for index, item in enumerate(raw, start=1)
        )
    milestones: list[Milestone] = []
    for expected, item in enumerate(raw, start=1):
        entry = _mapping(item, f"execution_plan[{expected}]")
        number = _positive_int(entry, "milestone")
        if number != expected:
            raise ContractError("execution_plan milestones must be continuous from 1")
        milestones.append(
            Milestone(number, _string(entry, "objective"), _strings(entry, "steps"))
        )
    return tuple(milestones)


def require_current_schema(spec: TaskSpec) -> None:
    if spec.schema_version != 2:
        raise ContractError("scheduled execution requires schema_version 2")


def _normalize_path(value: str) -> str:
    candidate = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or candidate.is_absolute()
        or ".." in candidate.parts
    ):
        raise ContractError(f"invalid repository path: {value}")
    normalized = candidate.as_posix().rstrip("/")
    if normalized in {"", "."}:
        raise ContractError(f"invalid repository path: {value}")
    return normalized


def _within(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def parse_issue_body(text: str) -> TaskSpec:
    if text.count(TASK_MARKER) != 1:
        raise ContractError("Issue body must contain exactly one duomac task block")
    match = _TASK_BLOCK.search(text)
    if match is None:
        raise ContractError("duomac task marker must be followed by one fenced YAML block")
    try:
        raw = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError as exc:
        raise ContractError(f"unable to parse task YAML: {exc}") from exc
    task = _mapping(raw, "task")
    schema_version = task.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in {1, 2}
    ):
        raise ContractError("schema_version must be 1 or 2")

    revision = _positive_int(task, "revision")
    role = _mapping(task.get("role"), "role")
    dispatcher = _string(role, "dispatcher")
    executor = _string(role, "executor")
    if dispatcher != "macbook" or executor != "mac-mini":
        raise ContractError("role must set dispatcher=macbook and executor=mac-mini")

    context = _mapping(task.get("context"), "context")
    context_commit = _string(context, "commit").lower()
    if _FULL_SHA.fullmatch(context_commit) is None:
        raise ContractError("context commit must be a full 40-character SHA")

    scope = _mapping(task.get("scope"), "scope")
    allowed_paths = tuple(_normalize_path(path) for path in _strings(scope, "allowed_paths"))
    delivery_mode = _string(task, "delivery_mode")
    if delivery_mode not in {"direct-main", "task-branch"}:
        raise ContractError("delivery_mode must be direct-main or task-branch")
    risk = _string(task, "risk")
    if risk not in {"low", "medium"}:
        raise ContractError("risk must be low or medium")

    return TaskSpec(
        schema_version=schema_version,
        revision=revision,
        dispatcher=dispatcher,
        executor=executor,
        objective=_string(task, "objective"),
        context_commit=context_commit,
        context_files=tuple(
            _normalize_path(path) for path in _strings(context, "files")
        ),
        decisions=_strings(context, "decisions"),
        acceptance=_strings(task, "acceptance"),
        allowed_paths=allowed_paths,
        out_of_scope=_strings(scope, "out_of_scope"),
        execution_plan=_milestones(task, schema_version),
        verification_profile=_string(task, "verification_profile"),
        delivery_mode=delivery_mode,
        risk=risk,
    )


def render_issue_body(spec: TaskSpec) -> str:
    payload = {
        "schema_version": spec.schema_version,
        "revision": spec.revision,
        "role": {"dispatcher": spec.dispatcher, "executor": spec.executor},
        "objective": spec.objective,
        "context": {
            "commit": spec.context_commit,
            "files": list(spec.context_files),
            "decisions": list(spec.decisions),
        },
        "acceptance": list(spec.acceptance),
        "scope": {
            "allowed_paths": list(spec.allowed_paths),
            "out_of_scope": list(spec.out_of_scope),
        },
        "execution_plan": (
            [milestone.objective for milestone in spec.execution_plan]
            if spec.schema_version == 1
            else [
                {
                    "milestone": milestone.number,
                    "objective": milestone.objective,
                    "steps": list(milestone.steps),
                }
                for milestone in spec.execution_plan
            ]
        ),
        "verification_profile": spec.verification_profile,
        "delivery_mode": spec.delivery_mode,
        "risk": spec.risk,
    }
    rendered = yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).rstrip()
    return f"{TASK_MARKER}\n```yaml\n{rendered}\n```\n"


def load_project_config(path: Path) -> ProjectConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"unable to read project config: {exc}") from exc
    return load_project_config_text(text)


def load_project_config_text(text: str) -> ProjectConfig:
    """Parse project configuration loaded from an authoritative Git object."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ContractError(f"unable to read project config: {exc}") from exc
    if raw.get("schema_version") != 1:
        raise ContractError("project schema_version must be 1")
    verification_raw = _mapping(raw.get("verification"), "verification")
    if not verification_raw:
        raise ContractError("verification must define at least one profile")
    verification: dict[str, tuple[str, ...]] = {}
    for name, value in verification_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ContractError("verification profile names must be non-empty strings")
        verification[name.strip()] = _strings(
            _mapping(value, f"verification.{name}"), "commands"
        )
    return ProjectConfig(
        default_base_branch=_string(raw, "default_base_branch"),
        protected_paths=tuple(
            _normalize_path(item) for item in _strings(raw, "protected_paths")
        ),
        max_changed_files=_positive_int(raw, "max_changed_files"),
        max_diff_lines=_positive_int(raw, "max_diff_lines"),
        verification=verification,
    )


def validate_task(spec: TaskSpec, project: ProjectConfig) -> None:
    if spec.verification_profile not in project.verification:
        raise ContractError(
            f"unknown verification profile: {spec.verification_profile}"
        )
    milestone_work = tuple(
        text
        for milestone in spec.execution_plan
        for text in (milestone.objective, *milestone.steps)
    )
    requested_work = "\n".join(
        (spec.objective, *spec.decisions, *spec.acceptance, *milestone_work)
    )
    if any(pattern.search(requested_work) for pattern in _FORBIDDEN_OPERATIONS):
        raise ContractError("operational or irreversible objective is not allowed")
    for allowed in spec.allowed_paths:
        if any(
            _within(allowed, protected) or _within(protected, allowed)
            for protected in project.protected_paths
        ):
            raise ContractError(f"allowed path overlaps protected path: {allowed}")
