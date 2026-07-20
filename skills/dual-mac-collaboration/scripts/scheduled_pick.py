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
    task_body_hash,
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
    ensure_directory,
    load_scheduled_config,
    select_candidate_result,
    validate_repository_target,
)
from issue_checkpoint import apply_event, validate_payload


_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SCHEDULED_BINDING_FIELDS = (
    "task_hash",
    "repository",
    "base_branch",
    "context_commit",
    "skill_commit",
    "base_commit",
    "revision",
    "execution_mode",
    "slot",
    "claim_id",
)
_INVALID_CONTRACT_DIAGNOSTIC = """<!-- duomac-scheduled-diagnostic:v1 -->
```yaml
type: blocked
reason: invalid-task-contract
next: publish-a-corrected-schema-v2-revision
```
"""


class PickError(RuntimeError):
    """Raised when a claim attempt cannot produce a trustworthy result."""

    def __init__(
        self, message: str, maintenance_actions: tuple[str, ...] = ()
    ) -> None:
        super().__init__(message)
        self.maintenance_actions = maintenance_actions


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(
            json.dumps(
                {
                    "claimed": False,
                    "outcome": "error",
                    "reason": "error",
                    "maintenance_actions": [],
                }
            )
        )
        self.exit(2)


@dataclass(frozen=True, slots=True)
class PickResult:
    claimed: bool
    outcome: str
    reason: str
    maintenance_actions: tuple[str, ...] = ()
    issue_url: str | None = None
    repo: str | None = None
    local_path: str | None = None
    slot: int | None = None
    claim_id: str | None = None
    base_commit: str | None = None

    def json_value(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def _unclaimed_result(
    reason: str, maintenance_actions: list[str] | tuple[str, ...] = ()
) -> PickResult:
    actions = tuple(maintenance_actions)
    return PickResult(
        claimed=False,
        outcome="maintenance" if actions else "clean-noop",
        reason=reason,
        maintenance_actions=actions,
    )


def _error_value(maintenance_actions: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "claimed": False,
        "outcome": "maintenance" if maintenance_actions else "error",
        "reason": "error",
        "maintenance_actions": list(maintenance_actions),
    }


def _action(kind: str, issue_url: str) -> str:
    return f"{kind}:{issue_url}"


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
            task_hash=task_body_hash(summary.body),
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
        task_hash=task_body_hash(summary.body),
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


def _apply_block_event(
    client: GhClient,
    ref: IssueRef,
    spec: TaskSpec,
    payload: dict[str, Any],
    *,
    issue_url: str,
    prior_labels: tuple[str, ...],
    action_prefix: str,
    maintenance_actions: list[str],
) -> None:
    prior_events = parse_issue_events(client.issue_comments(ref))
    payload_existed = any(event.payload == payload for event in prior_events)
    try:
        result = apply_event(client, ref, spec, payload)
    except GhError:
        try:
            snapshot = client.issue_snapshot(ref)
        except GhError:
            pass
        else:
            current_events = parse_issue_events(snapshot.comments)
            if not payload_existed and any(
                event.payload == payload for event in current_events
            ):
                maintenance_actions.append(
                    _action(f"{action_prefix}-comment-published", issue_url)
                )
            if (
                "duomac:blocked" not in prior_labels
                and "duomac:blocked" in snapshot.labels
            ):
                maintenance_actions.append(
                    _action(f"{action_prefix}-label-applied", issue_url)
                )
        raise
    if result["published"]:
        maintenance_actions.append(
            _action(f"{action_prefix}-comment-published", issue_url)
        )
    maintenance_actions.append(
        _action(f"{action_prefix}-label-applied", issue_url)
    )


def _block_invalid_ready(
    client: GhClient, invalid: InvalidReady, maintenance_actions: list[str]
) -> None:
    ref = IssueRef.parse(invalid.summary.url)
    if invalid.spec is not None:
        _apply_block_event(
            client,
            ref,
            invalid.spec,
            _blocked_payload(invalid.spec, "Scheduled execution requires schema_version 2"),
            issue_url=invalid.summary.url,
            prior_labels=invalid.summary.labels,
            action_prefix="invalid-ready-block",
            maintenance_actions=maintenance_actions,
        )
        return
    comments = client.issue_comments(ref)
    if not any(
        comment.get("body") == _INVALID_CONTRACT_DIAGNOSTIC
        for comment in comments
    ):
        client.comment(ref, _INVALID_CONTRACT_DIAGNOSTIC)
        maintenance_actions.append(
            _action("invalid-ready-block-comment-published", invalid.summary.url)
        )
    client.set_state_label(ref, "duomac:blocked")
    maintenance_actions.append(
        _action("invalid-ready-block-label-applied", invalid.summary.url)
    )


def _block_candidate(
    client: GhClient, candidate: Candidate, maintenance_actions: list[str]
) -> None:
    _apply_block_event(
        client,
        IssueRef.parse(candidate.issue_url),
        candidate.spec,
        _blocked_payload(
            candidate.spec,
            "Scheduled repository or context evidence failed validation",
        ),
        issue_url=candidate.issue_url,
        prior_labels=candidate.labels,
        action_prefix="candidate-block",
        maintenance_actions=maintenance_actions,
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
    payload: dict[str, Any],
) -> dict[str, Any]:
    validate_payload(payload)
    claim = {
        "issue_url": candidate.issue_url,
        "repo": candidate.repo,
        "local_path": str(target.local_path),
    }
    claim.update({field: payload[field] for field in _SCHEDULED_BINDING_FIELDS})
    return claim


def _same_task_binding(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(field) == right.get(field) for field in _SCHEDULED_BINDING_FIELDS)


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
    payload = starts[0].payload
    if payload.get("execution_mode") == "scheduled" and (
        payload.get("task_hash") != candidate.task_hash
        or payload.get("repository", "").casefold() != candidate.repo.casefold()
        or payload.get("context_commit") != candidate.spec.context_commit
    ):
        return None
    return starts[0]


def _repair_ready_claims(
    client: GhClient,
    config: ScheduledConfig,
    app_root: Path,
    candidates: tuple[Candidate, ...],
    maintenance_actions: list[str],
) -> bool:
    repaired = False
    for candidate in candidates:
        start = _current_task_start(candidate)
        if start is None:
            continue
        payload = start.payload
        target = _target_for(config, candidate.repo)
        client.set_state_label(IssueRef.parse(candidate.issue_url), "duomac:active")
        maintenance_actions.append(
            _action("ready-state-label-repaired", candidate.issue_url)
        )
        if payload["execution_mode"] == "scheduled":
            _write_claim(
                app_root,
                _claim_value(
                    candidate=candidate,
                    target=target,
                    payload=payload,
                ),
            )
            maintenance_actions.append(
                _action("ready-claim-projection-written", candidate.issue_url)
            )
        repaired = True
    return repaired


def _repair_active_claims(
    config: ScheduledConfig,
    app_root: Path,
    candidates: tuple[Candidate, ...],
    maintenance_actions: list[str],
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
                payload=payload,
            ),
        )
        maintenance_actions.append(
            _action("active-claim-projection-written", candidate.issue_url)
        )


def _state_requires_maintenance(state: GithubState) -> bool:
    if state.invalid_ready:
        return True
    for candidate in state.active_candidates:
        start = _current_task_start(candidate)
        if (
            start is not None
            and start.payload.get("execution_mode") == "scheduled"
        ):
            return True
    return any(_current_task_start(candidate) is not None for candidate in state.ready)


def _claim_is_authoritative(
    client: GhClient,
    candidate: Candidate,
    payload: dict[str, Any],
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
    return _same_task_binding(starts[0].payload, payload)


def _revalidate_selected(
    client: GhClient,
    candidate: Candidate,
    active: tuple[ActiveTask, ...],
    maximum: int,
) -> Candidate | None:
    snapshot = client.issue_snapshot(IssueRef.parse(candidate.issue_url))
    try:
        spec = parse_issue_body(snapshot.body)
        require_current_schema(spec)
        events = parse_issue_events(snapshot.comments)
    except (ContractError, GhError):
        return None
    fresh = Candidate(
        repo=candidate.repo,
        issue_url=candidate.issue_url,
        created_at=candidate.created_at,
        spec=spec,
        labels=snapshot.labels,
        events=events,
        state=snapshot.state,
        task_hash=task_body_hash(snapshot.body),
    )
    if (
        fresh.task_hash != candidate.task_hash
        or fresh.spec != candidate.spec
        or fresh.spec.revision != candidate.spec.revision
        or fresh.spec.context_commit != candidate.spec.context_commit
    ):
        return None
    result = select_candidate_result((fresh,), active, maximum)
    return fresh if result.candidate == fresh else None


def _same_candidate(left: Candidate, right: Candidate) -> bool:
    return (
        left.repo.casefold() == right.repo.casefold()
        and left.issue_url == right.issue_url
        and left.created_at == right.created_at
        and left.task_hash == right.task_hash
        and left.spec == right.spec
    )


def _preview_result(candidate: Candidate, target: RepositoryTarget, slot: int) -> PickResult:
    return PickResult(
        claimed=False,
        outcome="preview",
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
    client = GhClient()
    if not apply:
        state = _read_github_state(client, config)
        selection = select_candidate_result(
            state.ready, state.active, config.max_parallel_tasks
        )
        if selection.candidate is None:
            return _unclaimed_result(selection.reason)
        return _preview_result(
            selection.candidate,
            _target_for(config, selection.candidate.repo),
            slot,
        )

    initial_state = _read_github_state(client, config)
    initial_selection = select_candidate_result(
        initial_state.ready, initial_state.active, config.max_parallel_tasks
    )
    if (
        initial_selection.candidate is None
        and not _state_requires_maintenance(initial_state)
    ):
        return _unclaimed_result(initial_selection.reason)

    maintenance_actions: list[str] = []
    root = app_root.expanduser().resolve()
    if ensure_directory(root):
        maintenance_actions.append("application-root-created")

    try:
        lock_path = root / "dispatch.lock"
        with dispatch_lock(lock_path) as lock_created:
            if lock_created:
                maintenance_actions.append("dispatch-lock-file-created")
            state = _read_github_state(client, config)
            _repair_active_claims(
                config, root, state.active_candidates, maintenance_actions
            )
            if _repair_ready_claims(
                client, config, root, state.ready, maintenance_actions
            ):
                state = _read_github_state(client, config)

            blocked_invalid = bool(state.invalid_ready)
            for invalid in state.invalid_ready:
                _block_invalid_ready(client, invalid, maintenance_actions)
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
                return _unclaimed_result(reason, maintenance_actions)
            candidate = selection.candidate
            target = _target_for(config, candidate.repo)
            try:
                evidence = validate_repository_target(target, candidate.spec)
            except (ContractError, RepositoryValidationError):
                _block_candidate(client, candidate, maintenance_actions)
                return _unclaimed_result(
                    "invalid-candidates-blocked", maintenance_actions
                )

            skill_commit = _installed_skill_commit()
            refreshed_state = _read_github_state(client, config)
            refreshed_selection = select_candidate_result(
                refreshed_state.ready,
                refreshed_state.active,
                config.max_parallel_tasks,
            )
            if refreshed_selection.candidate is None:
                return _unclaimed_result(
                    refreshed_selection.reason, maintenance_actions
                )
            if not _same_candidate(candidate, refreshed_selection.candidate):
                return _unclaimed_result(
                    "invalid-candidates-blocked", maintenance_actions
                )
            candidate = refreshed_selection.candidate
            fresh = _revalidate_selected(
                client,
                candidate,
                refreshed_state.active,
                config.max_parallel_tasks,
            )
            if fresh is None:
                return _unclaimed_result(
                    "invalid-candidates-blocked", maintenance_actions
                )
            candidate = fresh
            claim_id = secrets.token_hex(20)
            payload = {
                "type": "task-start",
                "revision": candidate.spec.revision,
                "task_hash": candidate.task_hash,
                "repository": candidate.repo,
                "base_branch": evidence.project.default_base_branch,
                "context_commit": candidate.spec.context_commit,
                "skill_commit": skill_commit,
                "base_commit": evidence.base_commit,
                "plan_summary": [
                    milestone.objective
                    for milestone in candidate.spec.execution_plan
                ],
                "execution_mode": "scheduled",
                "slot": slot,
                "claim_id": claim_id,
            }
            claim = _claim_value(
                candidate=candidate, target=target, payload=payload
            )
            try:
                apply_event(
                    client,
                    IssueRef.parse(candidate.issue_url),
                    candidate.spec,
                    payload,
                )
            except ContractError:
                return _unclaimed_result(
                    "invalid-candidates-blocked", maintenance_actions
                )
            except GhError as exc:
                if not _claim_is_authoritative(client, candidate, payload):
                    raise PickError(
                        "GitHub task-start publication failed",
                        tuple(maintenance_actions),
                    ) from exc
            try:
                _write_claim(root, claim)
            except OSError:
                pass
            return PickResult(
                claimed=True,
                outcome="claimed",
                reason="selected",
                maintenance_actions=tuple(maintenance_actions),
                issue_url=candidate.issue_url,
                repo=candidate.repo,
                local_path=str(target.local_path),
                slot=slot,
                claim_id=claim_id,
                base_commit=evidence.base_commit,
            )
    except (ContractError, GhError, OSError, PickError) as exc:
        existing = (
            exc.maintenance_actions if isinstance(exc, PickError) else ()
        )
        combined = tuple(maintenance_actions) + tuple(
            action for action in existing if action not in maintenance_actions
        )
        raise PickError("Scheduled picker failed", combined) from exc


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
    except (ContractError, GhError, OSError, PickError) as exc:
        actions = exc.maintenance_actions if isinstance(exc, PickError) else ()
        print(json.dumps(_error_value(actions), sort_keys=True))
        return 1
    print(json.dumps(result.json_value(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
