#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Any

import yaml

from duomac_contracts import (
    ContractError,
    TaskSpec,
    parse_issue_body,
    require_current_schema,
)
from duomac_github import (
    EVENT_MARKER,
    GhClient,
    GhError,
    IssueEvent,
    IssueRef,
    current_revision_events,
    parse_issue_events,
)
from issue_checkpoint import _checkpoint_milestones, _require_authoritative_task_start


_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


def _within(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ContractError(f"unable to read delivery payload: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError("delivery payload must be a mapping")
    return value


def _strings(payload: dict[str, Any], field: str, *, allow_empty: bool = False) -> list[str]:
    value = payload.get(field)
    if not isinstance(value, list) or (not value and not allow_empty) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        qualifier = "a string list" if allow_empty else "a non-empty string list"
        raise ContractError(f"delivery {field} must be {qualifier}")
    return [item.strip() for item in value]


def validate_delivery(payload: dict[str, Any], spec: TaskSpec, state: str) -> None:
    if payload.get("type") != "delivery":
        raise ContractError("delivery type must be delivery")
    revision = payload.get("revision")
    if revision != spec.revision:
        raise ContractError(
            f"payload revision {revision} does not match Issue revision {spec.revision}"
        )
    mode = payload.get("delivery_mode")
    if mode != spec.delivery_mode:
        raise ContractError("payload delivery_mode does not match the Issue contract")
    if state == "completed" and spec.delivery_mode != "direct-main":
        raise ContractError("completed state is only valid for direct-main delivery")
    if state == "delivered" and spec.delivery_mode != "task-branch":
        raise ContractError("delivered state is only valid for task-branch delivery")
    commit = payload.get("commit")
    if not isinstance(commit, str) or _FULL_SHA.fullmatch(commit) is None:
        raise ContractError("delivery commit must be a full commit SHA")
    changed_paths = _strings(payload, "changed_paths")
    for path in changed_paths:
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or ".." in candidate.parts or "\\" in path:
            raise ContractError(f"invalid changed path: {path}")
        if not any(_within(path, allowed) for allowed in spec.allowed_paths):
            raise ContractError(f"changed path is outside the Issue contract: {path}")
    results = payload.get("acceptance_results")
    if not isinstance(results, list) or not results:
        raise ContractError("delivery acceptance_results must be a non-empty list")
    for result in results:
        if not isinstance(result, dict):
            raise ContractError("each acceptance result must be a mapping")
        for field in ("criterion", "evidence"):
            if not isinstance(result.get(field), str) or not result[field].strip():
                raise ContractError(f"acceptance result {field} must be non-empty")
        if result.get("status") != "met":
            raise ContractError("all acceptance results must be met before delivery")
    _strings(payload, "verification")
    _strings(payload, "remaining_risks", allow_empty=True)


def validate_checkpoint_completion(
    spec: TaskSpec, events: tuple[IssueEvent, ...]
) -> None:
    require_current_schema(spec)
    current = current_revision_events(events, spec.revision)
    _require_authoritative_task_start(current)
    if any(event.payload.get("type") == "blocked" for event in current):
        raise ContractError("current revision contains an unresolved blocked event")
    milestones = _checkpoint_milestones(current, spec)
    actual = set(milestones)
    expected = set(range(1, len(spec.execution_plan) + 1))
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise ContractError(
            "missing checkpoint milestones: " + ", ".join(map(str, missing))
        )
    if extra:
        raise ContractError(
            "unexpected checkpoint milestones: " + ", ".join(map(str, extra))
        )


def validate_delivery_history(
    payload: dict[str, Any], spec: TaskSpec, state: str, events: tuple[IssueEvent, ...]
) -> bool:
    current = current_revision_events(events, spec.revision)
    deliveries = [
        event.payload for event in current if event.payload.get("type") == "delivery"
    ]
    for delivery in deliveries:
        validate_delivery(delivery, spec, state)
    if not deliveries:
        return False
    if any(delivery != payload for delivery in deliveries):
        raise ContractError("current revision contains a conflicting delivery event")
    return True


def render_event(payload: dict[str, Any]) -> str:
    rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip()
    return f"{EVENT_MARKER}\n```yaml\n{rendered}\n```\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a guarded dual-Mac delivery")
    parser.add_argument("issue_url")
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--state", required=True, choices=("delivered", "completed"))
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    try:
        ref = IssueRef.parse(args.issue_url)
        payload = _load_payload(args.payload)
        client = GhClient()
        spec = parse_issue_body(client.issue_body(ref))
        validate_delivery(payload, spec, args.state)
        events = parse_issue_events(client.issue_comments(ref))
        validate_checkpoint_completion(spec, events)
        delivery_exists = validate_delivery_history(payload, spec, args.state, events)
        if not args.yes:
            print(
                json.dumps(
                    {
                        "applied": False,
                        "state": args.state,
                        "revision": spec.revision,
                        "payload": payload,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        issue_is_open = (
            args.state == "completed" and client.issue_state(ref) == "OPEN"
        )
        if not delivery_exists:
            client.comment(ref, render_event(payload))
        target_label = f"duomac:{args.state}"
        if not delivery_exists or not client.has_label(ref, target_label):
            client.set_state_label(ref, target_label)
        if issue_is_open:
            client.close(ref)
    except (ContractError, GhError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "applied": True,
                "state": args.state,
                "revision": spec.revision,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
