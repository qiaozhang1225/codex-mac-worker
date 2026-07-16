from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .assisted_merge import review_task
from .merge_policy import AUTOMATIC
from .references import IssueReference
from .store import EventStore


class AutoMergeBlocked(RuntimeError):
    """Raised before an automatic merge when any trusted gate is not satisfied."""


@dataclass(frozen=True, slots=True)
class AutomaticMergeResult:
    repo: str
    issue_number: int
    pr_number: int
    approved_head: str
    merge_commit_sha: str
    merged: bool


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_AUTOMATIC_LABELS = frozenset({"codex:awaiting-review", "codex:merging"})


def _confirmed_result(
    store: EventStore,
    reference: IssueReference,
    *,
    pr_number: int,
    expected_head: str,
    pull: dict[str, Any],
) -> AutomaticMergeResult | None:
    if not pull.get("merged_at"):
        return None
    observed_head = str(pull.get("head", {}).get("sha", "")).lower()
    if observed_head != expected_head:
        raise AutoMergeBlocked("merged PR head differs from expected head")
    operation = store.get_auto_merge(
        reference.repo, reference.number, pr_number, expected_head
    )
    if operation is None:
        raise AutoMergeBlocked("merged PR has no recorded auto-merge operation")
    merge_commit_sha = str(pull.get("merge_commit_sha", "")).lower()
    if not _FULL_SHA_RE.fullmatch(merge_commit_sha):
        raise AutoMergeBlocked("merged PR does not expose a full merge commit SHA")
    store.complete_auto_merge(
        reference.repo,
        reference.number,
        pr_number,
        expected_head,
        merge_commit_sha,
    )
    return AutomaticMergeResult(
        repo=reference.repo,
        issue_number=reference.number,
        pr_number=pr_number,
        approved_head=expected_head,
        merge_commit_sha=merge_commit_sha,
        merged=True,
    )


def _require_snapshot(
    github: Any,
    reference: IssueReference,
    *,
    pr_number: int,
    expected_head: str,
):
    snapshot = review_task(
        github,
        reference,
        allowed_lifecycle_labels=_AUTOMATIC_LABELS,
    )
    if snapshot.pr_number != pr_number:
        raise AutoMergeBlocked("reviewed PR number differs from requested PR")
    if snapshot.head_sha != expected_head:
        raise AutoMergeBlocked("reviewed PR head differs from expected head")
    if snapshot.ruleset_profile != AUTOMATIC:
        raise AutoMergeBlocked("repository does not use the automatic Ruleset profile")
    if not snapshot.gates.allowed:
        raise AutoMergeBlocked("; ".join(snapshot.gates.blockers))
    return snapshot


def automatic_merge_task(
    github: Any,
    store: EventStore,
    reference: IssueReference,
    *,
    pr_number: int,
    expected_head: str,
    merge_mode: str,
) -> AutomaticMergeResult:
    expected_head = expected_head.lower()
    if not _FULL_SHA_RE.fullmatch(expected_head):
        raise AutoMergeBlocked("expected head must be a full Git SHA")
    pull = github.get_pull_request(reference.repo, pr_number)
    confirmed = _confirmed_result(
        store,
        reference,
        pr_number=pr_number,
        expected_head=expected_head,
        pull=pull,
    )
    if confirmed is not None:
        return confirmed
    if merge_mode != AUTOMATIC:
        raise AutoMergeBlocked("local merge mode is not automatic")

    snapshot = _require_snapshot(
        github,
        reference,
        pr_number=pr_number,
        expected_head=expected_head,
    )
    store.begin_auto_merge(
        repo=reference.repo,
        issue_number=reference.number,
        pr_number=pr_number,
        task_hash=snapshot.task_hash,
        expected_head=expected_head,
    )
    try:
        github.mark_pull_request_ready(
            reference.repo,
            pr_number,
            expected_head=expected_head,
        )
        store.set_auto_merge_state(
            reference.repo,
            reference.number,
            pr_number,
            expected_head,
            state="ready",
        )
        _require_snapshot(
            github,
            reference,
            pr_number=pr_number,
            expected_head=expected_head,
        )
        github.merge_pull_request(
            reference.repo,
            pr_number,
            expected_head=expected_head,
        )
    except Exception as exc:
        store.set_auto_merge_state(
            reference.repo,
            reference.number,
            pr_number,
            expected_head,
            state="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        if isinstance(exc, AutoMergeBlocked):
            raise
        raise

    pull = github.get_pull_request(reference.repo, pr_number)
    confirmed = _confirmed_result(
        store,
        reference,
        pr_number=pr_number,
        expected_head=expected_head,
        pull=pull,
    )
    if confirmed is None:
        raise AutoMergeBlocked("GitHub did not confirm the automatic merge")
    return confirmed
