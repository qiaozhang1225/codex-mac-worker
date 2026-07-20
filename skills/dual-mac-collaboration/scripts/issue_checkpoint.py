#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml

from duomac_contracts import ContractError, TaskSpec, parse_issue_body
from duomac_github import (
    EVENT_MARKER,
    GhClient,
    GhError,
    IssueEvent,
    IssueRef,
    current_revision_events,
    parse_issue_events,
)


_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_CLAIM_ID = re.compile(r"^[0-9a-f]{40}$")
_TASK_HASH = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PASSING_RESULT = re.compile(r"(?:(?P<count>[0-9]+)\s+)?passed", re.IGNORECASE)
_FAILURE_MARKER = re.compile(
    r"\b(?:fail(?:s|ed|ing|ure|ures)?|error(?:s|ed|ing)?)\b", re.IGNORECASE
)


def _valid_base_branch(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if value in {".", "@"} or value.startswith(("/", ".")):
        return False
    if value.endswith(("/", ".", ".lock")):
        return False
    if ".." in value or "//" in value or "@{" in value:
        return False
    if any(ord(character) <= 32 or ord(character) == 127 for character in value):
        return False
    if any(character in "~^:?*[\\" for character in value):
        return False
    return all(
        component and not component.startswith(".") and not component.endswith(".lock")
        for component in value.split("/")
    )


def _mapping(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ContractError(f"unable to read checkpoint payload: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("checkpoint payload must be a mapping")
    return value


def _positive_revision(payload: dict[str, Any]) -> int:
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0:
        raise ContractError("checkpoint revision must be a positive integer")
    return revision


def _nonempty_strings(payload: dict[str, Any], field: str) -> None:
    value = payload.get(field)
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ContractError(f"checkpoint {field} must be a non-empty string list")


def _string_list(payload: dict[str, Any], field: str) -> None:
    value = payload.get(field)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ContractError(f"checkpoint {field} must be a string list")


def _validate_verification_evidence(items: list[str]) -> None:
    for item in items:
        if _FAILURE_MARKER.search(item) is not None:
            raise ContractError(
                "checkpoint verification must not contain a failure or error marker"
            )
        description, delimiter, result = item.rpartition(":")
        if not delimiter:
            raise ContractError(
                "checkpoint verification must contain explicitly passing results"
            )
        if not any(character.isalpha() for character in description):
            raise ContractError(
                "checkpoint verification requires a meaningful check description"
            )
        match = _PASSING_RESULT.fullmatch(result.strip())
        if match is None:
            raise ContractError(
                "checkpoint verification must contain explicitly passing results"
            )
        count = match.group("count")
        if count is not None and not count.strip("0"):
            raise ContractError(
                "checkpoint verification passing count must be greater than zero"
            )


def validate_payload(payload: dict[str, Any]) -> str:
    kind = payload.get("type")
    _positive_revision(payload)
    if kind == "task-start":
        for field in ("skill_commit", "base_commit"):
            value = payload.get(field)
            if not isinstance(value, str) or _FULL_SHA.fullmatch(value) is None:
                raise ContractError(f"task-start {field} must be a full commit SHA")
        _nonempty_strings(payload, "plan_summary")
        if payload.get("execution_mode") not in {"scheduled", "interactive"}:
            raise ContractError(
                "task-start execution_mode must be scheduled or interactive"
            )
        if payload["execution_mode"] == "scheduled":
            slot = payload.get("slot")
            claim_id = payload.get("claim_id")
            task_hash = payload.get("task_hash")
            repository = payload.get("repository")
            base_branch = payload.get("base_branch")
            context_commit = payload.get("context_commit")
            if (
                not isinstance(slot, int)
                or isinstance(slot, bool)
                or not 1 <= slot <= 3
            ):
                raise ContractError("scheduled task-start slot must be 1, 2, or 3")
            if (
                not isinstance(claim_id, str)
                or _CLAIM_ID.fullmatch(claim_id) is None
            ):
                raise ContractError(
                    "scheduled task-start claim_id must be 40 lowercase hex characters"
                )
            if not isinstance(task_hash, str) or _TASK_HASH.fullmatch(task_hash) is None:
                raise ContractError(
                    "scheduled task-start task_hash must be 64 lowercase hex characters"
                )
            if (
                not isinstance(repository, str)
                or _REPOSITORY.fullmatch(repository) is None
            ):
                raise ContractError(
                    "scheduled task-start repository must use OWNER/REPO format"
                )
            if not _valid_base_branch(base_branch):
                raise ContractError(
                    "scheduled task-start base_branch must be a non-empty branch name"
                )
            if (
                not isinstance(context_commit, str)
                or _FULL_SHA.fullmatch(context_commit) is None
            ):
                raise ContractError(
                    "scheduled task-start context_commit must be a full commit SHA"
                )
        return "duomac:active"
    if kind == "checkpoint":
        milestone = payload.get("milestone")
        if not isinstance(milestone, int) or isinstance(milestone, bool) or milestone <= 0:
            raise ContractError("checkpoint milestone must be a positive integer")
        _nonempty_strings(payload, "completed")
        _string_list(payload, "commits")
        if not payload["commits"] or any(
            _FULL_SHA.fullmatch(item) is None for item in payload["commits"]
        ):
            raise ContractError(
                "checkpoint commits must contain at least one full commit SHA"
            )
        _nonempty_strings(payload, "verification")
        _validate_verification_evidence(payload["verification"])
        if payload.get("scope_status") != "within-scope":
            raise ContractError("checkpoint scope_status must be within-scope")
        _nonempty_strings(payload, "next")
        _string_list(payload, "blockers")
        if payload["blockers"]:
            raise ContractError("checkpoint blockers must be empty")
        return "duomac:active"
    if kind == "blocked":
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ContractError("blocked reason must be a non-empty string")
        _string_list(payload, "completed")
        _nonempty_strings(payload, "next")
        return "duomac:blocked"
    raise ContractError("checkpoint type must be task-start, checkpoint, or blocked")


def render_event(payload: dict[str, Any]) -> str:
    rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip()
    return f"{EVENT_MARKER}\n```yaml\n{rendered}\n```\n"


def _result(
    payload: dict[str, Any], target_label: str, *, published: bool, repaired: bool
) -> dict[str, Any]:
    return {
        "published": published,
        "repaired": repaired,
        "type": payload["type"],
        "revision": payload["revision"],
        "state_label": target_label,
    }


def _current_events(
    client: GhClient, ref: IssueRef, revision: int
) -> tuple[IssueEvent, ...]:
    events = parse_issue_events(client.issue_comments(ref))
    return current_revision_events(events, revision)


def _validate_task_start(
    events: tuple[IssueEvent, ...], payload: dict[str, Any]
) -> bool:
    starts = [event for event in events if event.payload.get("type") == "task-start"]
    if not starts:
        return False
    for event in starts:
        validate_payload(event.payload)
    claim_id = payload.get("claim_id")
    if any(event.payload.get("claim_id") != claim_id for event in starts):
        raise ContractError(
            "current Issue revision already has a task-start from a different claim"
        )
    if payload.get("execution_mode") == "scheduled":
        binding_fields = (
            "revision",
            "task_hash",
            "repository",
            "base_branch",
            "context_commit",
            "skill_commit",
            "base_commit",
            "execution_mode",
            "slot",
            "claim_id",
        )
        expected = tuple(payload.get(field) for field in binding_fields)
        if any(
            tuple(event.payload.get(field) for field in binding_fields) != expected
            for event in starts
        ):
            raise ContractError(
                "current Issue revision task-start has a different task binding"
            )
    return True


def _require_authoritative_task_start(events: tuple[IssueEvent, ...]) -> IssueEvent:
    starts = [event for event in events if event.payload.get("type") == "task-start"]
    if len(starts) != 1:
        raise ContractError(
            "checkpoint requires exactly one current-revision task-start"
        )
    validate_payload(starts[0].payload)
    start_index = next(
        index for index, event in enumerate(events) if event is starts[0]
    )
    if any(
        event.payload.get("type") == "checkpoint"
        for event in events[:start_index]
    ):
        raise ContractError("task-start must precede checkpoint history")
    return starts[0]


def _checkpoint_milestones(
    events: tuple[IssueEvent, ...], spec: TaskSpec
) -> list[Any]:
    checkpoints = [
        event for event in events if event.payload.get("type") == "checkpoint"
    ]
    for event in checkpoints:
        validate_payload(event.payload)
    existing_milestones = [event.payload.get("milestone") for event in checkpoints]
    if any(
        not isinstance(milestone, int)
        or isinstance(milestone, bool)
        or milestone <= 0
        for milestone in existing_milestones
    ):
        raise ContractError("existing checkpoint milestone must be a positive integer")
    if existing_milestones != list(range(1, len(existing_milestones) + 1)):
        raise ContractError("existing checkpoint milestones must be continuous from 1")
    declared_milestones = {milestone.number for milestone in spec.execution_plan}
    for milestone in existing_milestones:
        if milestone not in declared_milestones:
            raise ContractError(
                f"historical checkpoint milestone {milestone} is not declared "
                "by the current execution plan"
            )
    return existing_milestones


def _validate_checkpoint_order(
    existing_milestones: list[Any], spec: TaskSpec, payload: dict[str, Any]
) -> None:
    expected = len(existing_milestones) + 1
    if payload["milestone"] != expected:
        raise ContractError(f"next checkpoint must be milestone {expected}")
    if expected > len(spec.execution_plan):
        raise ContractError("all declared milestones already have checkpoints")


def apply_event(
    client: GhClient,
    ref: IssueRef,
    spec: TaskSpec,
    payload: dict[str, Any],
) -> dict[str, Any]:
    target_label = validate_payload(payload)
    if payload["revision"] != spec.revision:
        raise ContractError(
            f"payload revision {payload['revision']} does not match Issue revision {spec.revision}"
        )

    events = _current_events(client, ref, spec.revision)
    kind = payload["type"]
    if kind == "checkpoint":
        _require_authoritative_task_start(events)
    existing_milestones = (
        _checkpoint_milestones(events, spec) if kind == "checkpoint" else []
    )
    if kind == "task-start":
        repaired = _validate_task_start(events, payload)
    else:
        repaired = any(event.payload == payload for event in events)

    if repaired:
        client.set_state_label(ref, target_label)
        return _result(payload, target_label, published=False, repaired=True)

    if kind == "checkpoint":
        _validate_checkpoint_order(existing_milestones, spec, payload)

    client.comment(ref, render_event(payload))
    client.set_state_label(ref, target_label)
    return _result(payload, target_label, published=True, repaired=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish dual-Mac task evidence")
    parser.add_argument("issue_url")
    parser.add_argument("--payload", required=True, type=Path)
    args = parser.parse_args()

    try:
        ref = IssueRef.parse(args.issue_url)
        payload = _mapping(args.payload)
        client = GhClient()
        spec = parse_issue_body(client.issue_body(ref))
        result = apply_event(client, ref, spec, payload)
    except (ContractError, GhError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
