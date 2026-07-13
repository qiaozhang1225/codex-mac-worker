from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any

import yaml


TASK_MARKER = "<!-- codex-task:v1 -->"
COMMAND_MARKER = "<!-- codex-command:v1 -->"
REPOSITORY_PROBE_MARKER = "<!-- codex-repository-probe:v1 -->"
REPOSITORY_ATTESTATION_MARKER = "<!-- codex-worker-readiness:v1 -->"
_BLOCK_RE = re.compile(
    rf"{re.escape(TASK_MARKER)}\s*```yaml\s*(.*?)\s*```",
    re.DOTALL,
)
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_COMMAND_BLOCK_RE = re.compile(
    rf"{re.escape(COMMAND_MARKER)}\s*```yaml\s*(.*?)\s*```",
    re.DOTALL,
)
_COMMAND_ACTIONS = {"revise", "pause", "resume", "retry", "cancel"}
_PROBE_BLOCK_RE = re.compile(
    rf"{re.escape(REPOSITORY_PROBE_MARKER)}\s*```yaml\s*(.*?)\s*```",
    re.DOTALL,
)
_ATTESTATION_BLOCK_RE = re.compile(
    rf"{re.escape(REPOSITORY_ATTESTATION_MARKER)}\s*```yaml\s*(.*?)\s*```",
    re.DOTALL,
)
_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ProtocolError(ValueError):
    """Raised when a machine-readable task or command is invalid."""


@dataclass(frozen=True, slots=True)
class TaskSpec:
    schema_version: int
    context_commit: str
    base_branch: str
    objective: str
    acceptance: tuple[str, ...]
    context_files: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    verification_profile: str
    risk: str
    canonical_yaml: str
    task_hash: str


@dataclass(frozen=True, slots=True)
class CommandSpec:
    command_id: str
    issue_number: int
    action: str
    requirements: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RepositoryProbe:
    probe_id: str
    default_head: str
    project_config_hash: str


@dataclass(frozen=True, slots=True)
class RepositoryAttestation:
    probe_id: str
    worker_id: str
    default_head: str
    project_config_hash: str
    attested_at: str


def _string_list(data: dict[str, Any], key: str, *, required: bool = True) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or (required and not value):
        raise ProtocolError(f"{key} must be a non-empty list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ProtocolError(f"{key} entries must be non-empty strings")
    return tuple(item.strip() for item in value)


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{key} must be a non-empty string")
    return value.strip()


def _machine_mapping(body: str, pattern: re.Pattern[str], resource: str) -> dict[str, Any]:
    matches = pattern.findall(body)
    if len(matches) != 1:
        raise ProtocolError(f"{resource} must contain exactly one machine block")
    try:
        raw = yaml.safe_load(matches[0])
    except yaml.YAMLError as exc:
        raise ProtocolError(f"invalid {resource} YAML: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ProtocolError(f"{resource} schema_version must be 1")
    return raw


def _full_hex(raw: dict[str, Any], key: str, pattern: re.Pattern[str]) -> str:
    value = _required_string(raw, key).lower()
    if not pattern.fullmatch(value):
        raise ProtocolError(f"{key} has an invalid hexadecimal length")
    return value


def parse_task_body(body: str) -> TaskSpec:
    matches = _BLOCK_RE.findall(body)
    if len(matches) != 1:
        raise ProtocolError("task body must contain exactly one codex-task machine block")
    try:
        raw = yaml.safe_load(matches[0])
    except yaml.YAMLError as exc:
        raise ProtocolError(f"invalid task YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProtocolError("task YAML must be a mapping")
    if raw.get("schema_version") != 1:
        raise ProtocolError("schema_version must be 1")

    context_commit = _required_string(raw, "context_commit").lower()
    if not _FULL_SHA_RE.fullmatch(context_commit):
        raise ProtocolError("context_commit must be a full 40-character Git SHA")

    normalized = {
        "acceptance": list(_string_list(raw, "acceptance")),
        "allowed_paths": list(_string_list(raw, "allowed_paths")),
        "base_branch": _required_string(raw, "base_branch"),
        "context_commit": context_commit,
        "context_files": list(_string_list(raw, "context_files")),
        "objective": _required_string(raw, "objective"),
        "risk": _required_string(raw, "risk"),
        "schema_version": 1,
        "verification_profile": _required_string(raw, "verification_profile"),
    }
    canonical = yaml.safe_dump(
        normalized,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
    )
    return TaskSpec(
        schema_version=1,
        context_commit=context_commit,
        base_branch=normalized["base_branch"],
        objective=normalized["objective"],
        acceptance=tuple(normalized["acceptance"]),
        context_files=tuple(normalized["context_files"]),
        allowed_paths=tuple(normalized["allowed_paths"]),
        verification_profile=normalized["verification_profile"],
        risk=normalized["risk"],
        canonical_yaml=canonical,
        task_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def render_command_comment(
    *,
    action: str,
    issue_number: int,
    requirements: tuple[str, ...],
    command_id: str,
) -> str:
    if action not in _COMMAND_ACTIONS:
        raise ProtocolError(f"unsupported command action: {action}")
    payload = {
        "schema_version": 1,
        "command_id": command_id,
        "issue_number": issue_number,
        "action": action,
        "requirements": list(requirements),
    }
    machine = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip()
    return f"{COMMAND_MARKER}\n```yaml\n{machine}\n```\n"


def parse_command_comment(body: str) -> CommandSpec:
    matches = _COMMAND_BLOCK_RE.findall(body)
    if len(matches) != 1:
        raise ProtocolError("comment must contain exactly one codex-command machine block")
    try:
        raw = yaml.safe_load(matches[0])
    except yaml.YAMLError as exc:
        raise ProtocolError(f"invalid command YAML: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ProtocolError("command schema_version must be 1")
    command_id = _required_string(raw, "command_id")
    action = _required_string(raw, "action")
    if action not in _COMMAND_ACTIONS:
        raise ProtocolError(f"unsupported command action: {action}")
    issue_number = raw.get("issue_number")
    if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number <= 0:
        raise ProtocolError("issue_number must be a positive integer")
    requirements_raw = raw.get("requirements", [])
    if not isinstance(requirements_raw, list) or not all(
        isinstance(item, str) and item.strip() for item in requirements_raw
    ):
        raise ProtocolError("requirements must be a string list")
    if action == "revise" and not requirements_raw:
        raise ProtocolError("revise requires at least one requirement")
    return CommandSpec(
        command_id=command_id,
        issue_number=issue_number,
        action=action,
        requirements=tuple(item.strip() for item in requirements_raw),
    )


def render_repository_probe(
    *,
    probe_id: str,
    default_head: str,
    project_config_hash: str,
) -> str:
    payload = {
        "schema_version": 1,
        "probe_id": probe_id,
        "default_head": default_head,
        "project_config_hash": project_config_hash,
    }
    machine = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip()
    return f"{REPOSITORY_PROBE_MARKER}\n```yaml\n{machine}\n```\n"


def parse_repository_probe(body: str) -> RepositoryProbe:
    raw = _machine_mapping(body, _PROBE_BLOCK_RE, "repository probe")
    return RepositoryProbe(
        probe_id=_required_string(raw, "probe_id"),
        default_head=_full_hex(raw, "default_head", _FULL_SHA_RE),
        project_config_hash=_full_hex(raw, "project_config_hash", _HASH_RE),
    )


def render_repository_attestation(
    *,
    probe_id: str,
    worker_id: str,
    default_head: str,
    project_config_hash: str,
    attested_at: str,
) -> str:
    payload = {
        "schema_version": 1,
        "probe_id": probe_id,
        "worker_id": worker_id,
        "default_head": default_head,
        "project_config_hash": project_config_hash,
        "attested_at": attested_at,
    }
    machine = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip()
    return f"{REPOSITORY_ATTESTATION_MARKER}\n```yaml\n{machine}\n```\n"


def parse_repository_attestation(body: str) -> RepositoryAttestation:
    raw = _machine_mapping(body, _ATTESTATION_BLOCK_RE, "repository attestation")
    return RepositoryAttestation(
        probe_id=_required_string(raw, "probe_id"),
        worker_id=_required_string(raw, "worker_id"),
        default_head=_full_hex(raw, "default_head", _FULL_SHA_RE),
        project_config_hash=_full_hex(raw, "project_config_hash", _HASH_RE),
        attested_at=_required_string(raw, "attested_at"),
    )
