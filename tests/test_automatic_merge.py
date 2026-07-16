from __future__ import annotations

from pathlib import Path

import pytest

from codex_mac_worker.automatic_merge import AutoMergeBlocked, automatic_merge_task
from codex_mac_worker.durable_github import DurableGitHub
from codex_mac_worker.merge_policy import ruleset_payload
from codex_mac_worker.references import IssueReference
from codex_mac_worker.store import EventStore

from .test_assisted_merge import ReviewGitHub


REF = IssueReference("owner/repo", 12)
HEAD = "c" * 40


class AutoMergeGitHub(ReviewGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.ruleset = ruleset_payload("automatic") | {"id": 1}
        self.merge_calls = 0

    def merge_pull_request(
        self, repo: str, pr_number: int, *, expected_head: str
    ) -> dict:
        self.merge_calls += 1
        return super().merge_pull_request(
            repo, pr_number, expected_head=expected_head
        )


def durable(tmp_path: Path, remote: AutoMergeGitHub) -> tuple[DurableGitHub, EventStore]:
    store = EventStore(tmp_path / "worker.sqlite3")
    return DurableGitHub(remote, store), store


def test_auto_merge_requires_both_trusted_signals(tmp_path: Path) -> None:
    remote = AutoMergeGitHub()
    github, store = durable(tmp_path, remote)

    with pytest.raises(AutoMergeBlocked, match="local merge mode"):
        automatic_merge_task(
            github,
            store,
            REF,
            pr_number=44,
            expected_head=HEAD,
            merge_mode="manual",
        )

    assert remote.writes == []


def test_auto_merge_marks_ready_rechecks_and_squashes(tmp_path: Path) -> None:
    remote = AutoMergeGitHub()
    github, store = durable(tmp_path, remote)

    result = automatic_merge_task(
        github,
        store,
        REF,
        pr_number=44,
        expected_head=HEAD,
        merge_mode="automatic",
    )

    assert result.merged is True
    assert remote.writes == ["ready", "merge"]
    assert remote.merge_payload == {"merge_method": "squash", "sha": HEAD}
    operation = store.get_auto_merge("owner/repo", 12, 44, HEAD)
    assert operation is not None
    assert operation["state"] == "completed"
    assert operation["merge_commit_sha"] == "e" * 40


def test_auto_merge_reconciles_lost_success_without_second_merge(
    tmp_path: Path,
) -> None:
    class LostResponseGitHub(AutoMergeGitHub):
        def __init__(self) -> None:
            super().__init__()
            self.fail_once = True

        def merge_pull_request(
            self, repo: str, pr_number: int, *, expected_head: str
        ) -> dict:
            result = super().merge_pull_request(
                repo, pr_number, expected_head=expected_head
            )
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("connection dropped after merge")
            return result

    remote = LostResponseGitHub()
    github, store = durable(tmp_path, remote)
    with pytest.raises(RuntimeError, match="connection dropped"):
        automatic_merge_task(
            github,
            store,
            REF,
            pr_number=44,
            expected_head=HEAD,
            merge_mode="automatic",
        )

    result = automatic_merge_task(
        github,
        store,
        REF,
        pr_number=44,
        expected_head=HEAD,
        merge_mode="automatic",
    )

    assert result.merged is True
    assert remote.merge_calls == 1


def test_auto_merge_blocks_head_change_after_ready(tmp_path: Path) -> None:
    class DriftingGitHub(AutoMergeGitHub):
        def mark_pull_request_ready(self, repo: str, pr_number: int) -> dict:
            result = super().mark_pull_request_ready(repo, pr_number)
            self.pull["head"]["sha"] = "d" * 40
            return result

    remote = DriftingGitHub()
    github, store = durable(tmp_path, remote)

    with pytest.raises(AutoMergeBlocked, match="head"):
        automatic_merge_task(
            github,
            store,
            REF,
            pr_number=44,
            expected_head=HEAD,
            merge_mode="automatic",
        )

    assert remote.merge_calls == 0


@pytest.mark.parametrize(
    ("mutation", "blocker"),
    [
        ("different_app", "GitHub App"),
        ("unresolved_thread", "review threads"),
        ("failed_check", "checks"),
        ("high_risk", "risk"),
    ],
)
def test_auto_merge_preserves_assisted_review_safety_gates(
    tmp_path: Path, mutation: str, blocker: str
) -> None:
    remote = AutoMergeGitHub()
    mutated = ReviewGitHub.with_mutation(mutation)
    remote.__dict__.update(mutated.__dict__)
    remote.ruleset = ruleset_payload("automatic") | {"id": 1}
    github, store = durable(tmp_path, remote)

    with pytest.raises(AutoMergeBlocked, match=blocker):
        automatic_merge_task(
            github,
            store,
            REF,
            pr_number=44,
            expected_head=HEAD,
            merge_mode="automatic",
        )

    assert remote.writes == []


def test_auto_merge_rejects_manual_ruleset(tmp_path: Path) -> None:
    remote = AutoMergeGitHub()
    remote.ruleset = ruleset_payload("manual") | {"id": 1}
    github, store = durable(tmp_path, remote)

    with pytest.raises(AutoMergeBlocked, match="automatic Ruleset"):
        automatic_merge_task(
            github,
            store,
            REF,
            pr_number=44,
            expected_head=HEAD,
            merge_mode="automatic",
        )

    assert remote.writes == []
