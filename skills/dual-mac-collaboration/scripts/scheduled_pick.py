#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from typing import Any

from duomac_contracts import (
    ContractError,
    TaskSpec,
    parse_issue_body,
    require_current_schema,
)
from duomac_github import (
    GhClient,
    GhError,
    IssueEvent,
    IssueRef,
    IssueSummary,
    current_revision_events,
    parse_issue_events,
)
from duomac_scheduled import (
    ActiveTask,
    Candidate,
    RepositoryTarget,
    RepositoryValidationError,
    ScheduledConfig,
    dispatch_lock,
    load_scheduled_config,
    select_candidate_result,
    validate_repository_target,
)
from issue_checkpoint import apply_event, validate_payload


_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_INVALID_CONTRACT_DIAGNOSTIC = """<!-- duomac-scheduled-diagnostic:v1 -->
```yaml
type: blocked
reason: invalid-task-contract
next: publish-a-corrected-schema-v2-revision
```
"""


class PickError(RuntimeError):
    """Raised when a claim attempt cannot produce a trustworthy result."""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(json.dumps({"claimed": False, "reason": "error"}))
        self.exit(2)


@dataclass(frozen=True, slots=True)
class PickResult:
    claimed: bool
    reason: str
    issue_url: str | None = None
    repo: str | None = None
    local_path: str | None = None
    slot: int | None = None
    claim_id: str | None = None
    base_commit: str | None = None

    def json_value(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True, slots=True)
class InvalidReady:
    summary: IssueSummary
    spec: TaskSpec | None


@dataclass(frozen=True, slots=True)
class GithubState:
    ready: tuple[Candidate, ...]
    active: tuple[ActiveTask, ...]
    active_candidates: tuple[Candidate, ...]
    invalid_ready: tuple[InvalidReady, ...]


def _events(client: GhClient, summary: IssueSummary) -> tuple[IssueEvent, ...]:
    return parse_issue_events(client.issue_comments(IssueRef.parse(summary.url)))


def _active_issue(
    client: GhClient, summary: IssueSummary
) -> tuple[ActiveTask, Candidate]:
    try:
        spec = parse_issue_body(summary.body)
        require_current_schema(spec)
        all_events = _events(client, summary)
        events = current_revision_events(all_events, spec.revision)
        starts = [event for event in events if event.payload.get("type") == "task-start"]
        if len(starts) != 1:
            raise PickError("active Issue lacks one authoritative task-start")
        validate_payload(starts[0].payload)
    except (ContractError, GhError) as exc:
        raise PickError("active Issue evidence is invalid") from exc
    return (
        ActiveTask(summary.repo, spec.allowed_paths),
        Candidate(
            repo=summary.repo,
            issue_url=summary.url,
            created_at=summary.created_at,
            spec=spec,
            labels=summary.labels,
            events=all_events,
            state="open",
        ),
    )


def _ready_candidate(
    client: GhClient, summary: IssueSummary
) -> Candidate | InvalidReady:
    try:
        spec = parse_issue_body(summary.body)
    except ContractError:
        return InvalidReady(summary, None)
    try:
        require_current_schema(spec)
    except ContractError:
        return InvalidReady(summary, spec)
    return Candidate(
        repo=summary.repo,
        issue_url=summary.url,
        created_at=summary.created_at,
        spec=spec,
        labels=summary.labels,
        events=_events(client, summary),
        state="open",
    )


def _read_github_state(client: GhClient, config: ScheduledConfig) -> GithubState:
    ready: list[Candidate] = []
    active: list[ActiveTask] = []
    active_candidates: list[Candidate] = []
    invalid: list[InvalidReady] = []
    for target in config.repositories:
        for summary in client.list_issues(target.github, "duomac:active"):
            task, candidate = _active_issue(client, summary)
            active.append(task)
            active_candidates.append(candidate)
        for summary in client.list_issues(target.github, "duomac:ready"):
            candidate = _ready_candidate(client, summary)
            if isinstance(candidate, InvalidReady):
                invalid.append(candidate)
            else:
                ready.append(candidate)
    return GithubState(
        tuple(ready), tuple(active), tuple(active_candidates), tuple(invalid)
    )


def _target_for(config: ScheduledConfig, repo: str) -> RepositoryTarget:
    matches = [
        target for target in config.repositories if target.github.casefold() == repo.casefold()
    ]
    if len(matches) != 1:
        raise PickError("Issue repository is not configured exactly once")
    return matches[0]


def _blocked_payload(spec: TaskSpec, reason: str) -> dict[str, Any]:
    return {
        "type": "blocked",
        "revision": spec.revision,
        "reason": reason,
        "completed": [],
        "next": ["MacBook must publish a corrected schema v2 Issue revision"],
    }


def _block_invalid_ready(client: GhClient, invalid: InvalidReady) -> None:
    ref = IssueRef.parse(invalid.summary.url)
    if invalid.spec is not None:
        apply_event(
            client,
            ref,
            invalid.spec,
            _blocked_payload(invalid.spec, "Scheduled execution requires schema_version 2"),
        )
        return
    comments = client.issue_comments(ref)
    if not any(
        comment.get("body") == _INVALID_CONTRACT_DIAGNOSTIC
        for comment in comments
    ):
        client.comment(ref, _INVALID_CONTRACT_DIAGNOSTIC)
    client.set_state_label(ref, "duomac:blocked")


def _block_candidate(client: GhClient, candidate: Candidate) -> None:
    apply_event(
        client,
        IssueRef.parse(candidate.issue_url),
        candidate.spec,
        _blocked_payload(
            candidate.spec,
            "Scheduled repository or context evidence failed validation",
        ),
    )


def _installed_skill_commit() -> str:
    skill_root = Path(__file__).resolve().parents[1]
    source_file = skill_root / ".source-commit"
    try:
        if source_file.is_file():
            value = source_file.read_text(encoding="utf-8").strip().lower()
        else:
            env = os.environ.copy()
            env["GIT_TERMINAL_PROMPT"] = "0"
            result = subprocess.run(
                ["git", "-C", str(skill_root), "rev-parse", "HEAD"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            value = result.stdout.strip().lower() if result.returncode == 0 else ""
    except OSError as exc:
        raise PickError("installed skill commit is unavailable") from exc
    if _FULL_SHA.fullmatch(value) is None:
        raise PickError("installed skill commit is invalid")
    return value


def _claim_value(
    *,
    candidate: Candidate,
    target: RepositoryTarget,
    slot: int,
    claim_id: str,
    base_commit: str,
) -> dict[str, Any]:
    return {
        "issue_url": candidate.issue_url,
        "repo": candidate.repo,
        "local_path": str(target.local_path),
        "slot": slot,
        "claim_id": claim_id,
        "base_commit": base_commit,
    }


def _write_claim(app_root: Path, claim: dict[str, Any]) -> None:
    claim_id = claim["claim_id"]
    if not isinstance(claim_id, str) or re.fullmatch(r"[0-9a-f]{40}", claim_id) is None:
        raise PickError("authoritative claim ID is invalid")
    claims = app_root / "claims"
    claims.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = claims / f"{claim_id}.json"
    temporary = claims / f".{claim_id}.{os.getpid()}.tmp"
    temporary.write_text(
        json.dumps(claim, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(destination)


def _current_task_start(candidate: Candidate) -> IssueEvent | None:
    events = current_revision_events(candidate.events, candidate.spec.revision)
    if any(
        event.payload.get("type") in {"blocked", "delivery"} for event in events
    ):
        return None
    starts = [event for event in events if event.payload.get("type") == "task-start"]
    if len(starts) != 1:
        return None
    try:
        validate_payload(starts[0].payload)
    except ContractError:
        return None
    return starts[0]


def _repair_ready_claims(
    client: GhClient,
    config: ScheduledConfig,
    app_root: Path,
    candidates: tuple[Candidate, ...],
) -> bool:
    repaired = False
    for candidate in candidates:
        start = _current_task_start(candidate)
        if start is None:
            continue
        payload = start.payload
        target = _target_for(config, candidate.repo)
        client.set_state_label(IssueRef.parse(candidate.issue_url), "duomac:active")
        if payload["execution_mode"] == "scheduled":
            _write_claim(
                app_root,
                _claim_value(
                    candidate=candidate,
                    target=target,
                    slot=payload["slot"],
                    claim_id=payload["claim_id"],
                    base_commit=payload["base_commit"],
                ),
            )
        repaired = True
    return repaired


def _repair_active_claims(
    config: ScheduledConfig,
    app_root: Path,
    candidates: tuple[Candidate, ...],
) -> None:
    for candidate in candidates:
        start = _current_task_start(candidate)
        if start is None:
            raise PickError("active Issue lacks one authoritative task-start")
        payload = start.payload
        if payload["execution_mode"] != "scheduled":
            continue
        _write_claim(
            app_root,
            _claim_value(
                candidate=candidate,
                target=_target_for(config, candidate.repo),
                slot=payload["slot"],
                claim_id=payload["claim_id"],
                base_commit=payload["base_commit"],
            ),
        )


def _claim_is_authoritative(
    client: GhClient,
    candidate: Candidate,
    claim_id: str,
) -> bool:
    events = current_revision_events(
        parse_issue_events(
            client.issue_comments(IssueRef.parse(candidate.issue_url))
        ),
        candidate.spec.revision,
    )
    starts = [event for event in events if event.payload.get("type") == "task-start"]
    if len(starts) != 1:
        return False
    try:
        validate_payload(starts[0].payload)
    except ContractError:
        return False
    return starts[0].payload.get("claim_id") == claim_id


def _preview_result(candidate: Candidate, target: RepositoryTarget, slot: int) -> PickResult:
    return PickResult(
        claimed=False,
        reason="preview",
        issue_url=candidate.issue_url,
        repo=candidate.repo,
        local_path=str(target.local_path),
        slot=slot,
    )


def pick(config_path: Path, app_root: Path, slot: int, apply: bool) -> PickResult:
    if not isinstance(slot, int) or isinstance(slot, bool) or slot not in {1, 2, 3}:
        raise PickError("slot must be 1, 2, or 3")
    config = load_scheduled_config(config_path)
    root = app_root.expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise PickError("application root must be a directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = GhClient()

    if apply:
        _read_github_state(client, config)

    with dispatch_lock(root / "dispatch.lock"):
        state = _read_github_state(client, config)
        if not apply:
            selection = select_candidate_result(
                state.ready, state.active, config.max_parallel_tasks
            )
            if selection.candidate is None:
                return PickResult(False, selection.reason)
            return _preview_result(
                selection.candidate,
                _target_for(config, selection.candidate.repo),
                slot,
            )

        _repair_active_claims(config, root, state.active_candidates)
        if _repair_ready_claims(client, config, root, state.ready):
            state = _read_github_state(client, config)

        blocked_invalid = bool(state.invalid_ready)
        for invalid in state.invalid_ready:
            _block_invalid_ready(client, invalid)
        if blocked_invalid:
            state = _read_github_state(client, config)

        selection = select_candidate_result(
            state.ready, state.active, config.max_parallel_tasks
        )
        if selection.candidate is None:
            reason = (
                "invalid-candidates-blocked"
                if blocked_invalid and selection.reason == "no-ready"
                else selection.reason
            )
            return PickResult(False, reason)
        candidate = selection.candidate
        target = _target_for(config, candidate.repo)
        try:
            evidence = validate_repository_target(target, candidate.spec)
        except (ContractError, RepositoryValidationError):
            _block_candidate(client, candidate)
            return PickResult(False, "invalid-candidates-blocked")

        claim_id = secrets.token_hex(20)
        claim = _claim_value(
            candidate=candidate,
            target=target,
            slot=slot,
            claim_id=claim_id,
            base_commit=evidence.base_commit,
        )
        payload = {
            "type": "task-start",
            "revision": candidate.spec.revision,
            "skill_commit": _installed_skill_commit(),
            "base_commit": evidence.base_commit,
            "plan_summary": [
                milestone.objective for milestone in candidate.spec.execution_plan
            ],
            "execution_mode": "scheduled",
            "slot": slot,
            "claim_id": claim_id,
        }
        try:
            apply_event(
                client,
                IssueRef.parse(candidate.issue_url),
                candidate.spec,
                payload,
            )
        except GhError as exc:
            if not _claim_is_authoritative(client, candidate, claim_id):
                raise PickError("GitHub task-start publication failed") from exc
        try:
            _write_claim(root, claim)
        except OSError:
            pass
        return PickResult(claimed=True, reason="selected", **claim)


def main() -> int:
    parser = JsonArgumentParser(
        description="Preview or claim one Scheduled dual-Mac Issue"
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--app-root", required=True, type=Path)
    parser.add_argument("--slot", required=True, type=int, choices=(1, 2, 3))
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    try:
        result = pick(args.config, args.app_root, args.slot, args.yes)
    except (ContractError, GhError, OSError, PickError):
        print(json.dumps({"claimed": False, "reason": "error"}))
        return 1
    print(json.dumps(result.json_value(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
