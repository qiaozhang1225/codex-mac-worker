#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml

from duomac_contracts import ContractError, parse_issue_body
from duomac_github import EVENT_MARKER, GhClient, GhError, IssueRef


_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


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


def validate_payload(payload: dict[str, Any]) -> str:
    kind = payload.get("type")
    _positive_revision(payload)
    if kind == "task-start":
        for field in ("skill_commit", "base_commit"):
            value = payload.get(field)
            if not isinstance(value, str) or _FULL_SHA.fullmatch(value) is None:
                raise ContractError(f"task-start {field} must be a full commit SHA")
        _nonempty_strings(payload, "plan_summary")
        return "duomac:active"
    if kind == "checkpoint":
        milestone = payload.get("milestone")
        if not isinstance(milestone, int) or isinstance(milestone, bool) or milestone <= 0:
            raise ContractError("checkpoint milestone must be a positive integer")
        _nonempty_strings(payload, "completed")
        _string_list(payload, "commits")
        if any(_FULL_SHA.fullmatch(item) is None for item in payload["commits"]):
            raise ContractError("checkpoint commits must contain full commit SHAs")
        _nonempty_strings(payload, "verification")
        if payload.get("scope_status") != "within-scope":
            raise ContractError("checkpoint scope_status must be within-scope")
        _nonempty_strings(payload, "next")
        _string_list(payload, "blockers")
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish dual-Mac task evidence")
    parser.add_argument("issue_url")
    parser.add_argument("--payload", required=True, type=Path)
    args = parser.parse_args()

    try:
        ref = IssueRef.parse(args.issue_url)
        payload = _mapping(args.payload)
        target_label = validate_payload(payload)
        client = GhClient()
        spec = parse_issue_body(client.issue_body(ref))
        if payload["revision"] != spec.revision:
            raise ContractError(
                f"payload revision {payload['revision']} does not match Issue revision {spec.revision}"
            )
        client.set_state_label(ref, target_label)
        client.comment(ref, render_event(payload))
    except (ContractError, GhError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "published": True,
                "type": payload["type"],
                "revision": payload["revision"],
                "state_label": target_label,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

