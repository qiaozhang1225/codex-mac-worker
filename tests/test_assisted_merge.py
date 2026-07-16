from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from codex_mac_worker.protocol import (
    DeliveryMetadata,
    parse_task_body,
    render_delivery_block,
    render_repository_attestation,
    render_repository_probe,
)
from codex_mac_worker.references import IssueReference
from codex_mac_worker.repository_onboarding import ruleset_payload


PROJECT_CONFIG = """schema_version = 2
default_base_branch = "main"
worker_github_app_id = 777
allowed_risk_levels = ["low", "medium"]
protected_paths = [".github/workflows", ".env", "product/deploy"]
max_changed_files = 30
max_diff_lines = 3000
codex_attempt_timeout_minutes = 45
task_hard_timeout_minutes = 120
max_automatic_attempts = 2

[verification.fast]
commands = ["pytest -q"]
"""


def task_body(*, risk: str = "low") -> str:
    return f"""<!-- codex-task:v1 -->
```yaml
schema_version: 1
context_commit: {'a' * 40}
base_branch: main
objective: Update one bounded source file
acceptance:
  - Unit tests pass
context_files:
  - docs/spec.md
allowed_paths:
  - src/
verification_profile: fast
risk: {risk}
```
"""


class ReviewGitHub:
    def __init__(self) -> None:
        body = task_body()
        task_hash = parse_task_body(body).task_hash
        self.issue = {
            "number": 12,
            "body": body,
            "state": "open",
            "labels": [{"name": "codex:awaiting-review"}],
        }
        self.pull = {
            "number": 44,
            "html_url": "https://github.com/owner/repo/pull/44",
            "body": render_delivery_block(
                DeliveryMetadata(
                    issue_number=12,
                    task_hash=task_hash,
                    context_commit="a" * 40,
                    delivery_commit="c" * 40,
                    verification_profile="fast",
                    verification_passed=True,
                    model="gpt-test",
                    cli_version="codex-test",
                    acceptance_results=(
                        {
                            "criterion": "Unit tests pass",
                            "status": "met",
                            "evidence": "pytest -q passed",
                        },
                    ),
                    risks=(),
                    needs_human=(),
                )
            ),
            "base": {"ref": "main", "sha": "a" * 40},
            "head": {"ref": "codex/12-bounded", "sha": "c" * 40},
            "user": {"login": "worker-app[bot]", "type": "Bot"},
            "performed_via_github_app": {"id": 777, "slug": "worker-app"},
            "draft": True,
            "mergeable": True,
        }
        self.files = [
            {"filename": "src/result.ts", "status": "modified", "additions": 8, "deletions": 2}
        ]
        self.check_runs = [
            {"name": "test", "status": "completed", "conclusion": "success"}
        ]
        self.commit_status = {"statuses": []}
        self.threads: list[dict] = []
        self.ruleset = ruleset_payload() | {"id": 1}
        self.default_head = "a" * 40
        self.attested_head = "a" * 40
        self.attestation_app_id = 777
        self.writes: list[str] = []
        self.ready_calls = 0
        self.merge_payload: dict[str, str] | None = None
        self.comments: list[str] = []
        self.reviews: list[dict] = []
        self.authenticated_login = "qiaoz"

    @classmethod
    def happy_path(cls) -> "ReviewGitHub":
        return cls()

    @classmethod
    def with_mutation(cls, mutation: str) -> "ReviewGitHub":
        github = cls()
        if mutation == "failed_check":
            github.check_runs[0]["conclusion"] = "failure"
        elif mutation == "pending_check":
            github.check_runs[0].update(status="in_progress", conclusion=None)
        elif mutation == "unresolved_thread":
            github.threads = [
                {"isResolved": False, "comments": {"nodes": [{"url": "https://thread"}]}}
            ]
        elif mutation == "conflict":
            github.pull["mergeable"] = False
        elif mutation == "outside_path":
            github.files[0]["filename"] = "docs/outside.md"
        elif mutation == "protected_path":
            github.files[0]["filename"] = ".env"
        elif mutation == "high_risk":
            github.issue["body"] = task_body(risk="high")
        elif mutation == "task_hash_drift":
            github.pull["body"] = github.pull["body"].replace(
                parse_task_body(task_body()).task_hash, "b" * 64
            )
        elif mutation == "delivery_sha_drift":
            github.pull["body"] = github.pull["body"].replace("c" * 40, "d" * 40)
        elif mutation == "non_worker_branch":
            github.pull["head"]["ref"] = "feature/untrusted"
        elif mutation == "ruleset_drift":
            github.ruleset["enforcement"] = "disabled"
        elif mutation == "missing_required_check":
            github.ruleset["rules"].append(
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [{"context": "required-ci"}]
                    },
                }
            )
        elif mutation == "wrong_issue_state":
            github.issue["state"] = "closed"
        elif mutation == "credential_risk":
            github.pull["body"] = github.pull["body"].replace(
                "risks: []", "risks:\n- Credentials may be exposed during deployment"
            )
        elif mutation == "different_app":
            github.pull["performed_via_github_app"]["id"] = 999
        elif mutation == "alternate_app":
            github.pull["performed_via_github_app"]["id"] = 999
            github.attestation_app_id = 999
        elif mutation == "nullable_app_metadata":
            github.attestation_app_id = None
        else:
            raise AssertionError(mutation)
        return github

    def get_issue(self, repo: str, issue_number: int) -> dict:
        return deepcopy(self.issue)

    def list_pull_requests(self, repo: str, *, state: str = "open", **kwargs: object) -> list[dict]:
        if state == "open" and self.pull.get("merged_at"):
            return []
        summary = deepcopy(self.pull)
        summary.pop("mergeable", None)
        summary.pop("merged_at", None)
        summary.pop("merge_commit_sha", None)
        return [summary]

    def get_pull_request(self, repo: str, pr_number: int) -> dict:
        return deepcopy(self.pull)

    def list_pull_files(self, repo: str, pr_number: int) -> list[dict]:
        return deepcopy(self.files)

    def get_repository_file(self, repo: str, path: str, *, ref: str) -> str:
        assert path == ".codex-worker/project.toml"
        return PROJECT_CONFIG

    def list_check_runs(self, repo: str, sha: str) -> list[dict]:
        return deepcopy(self.check_runs)

    def get_combined_status(self, repo: str, sha: str) -> dict:
        return deepcopy(self.commit_status)

    def list_review_threads(self, repo: str, pr_number: int) -> list[dict]:
        return deepcopy(self.threads)

    def list_rulesets(self, repo: str) -> list[dict]:
        return [{"id": 1, "name": self.ruleset["name"]}]

    def get_ruleset(self, repo: str, ruleset_id: int) -> dict:
        return deepcopy(self.ruleset)

    def get_repository(self, repo: str) -> dict:
        return {"default_branch": "main"}

    def get_commit(self, repo: str, ref: str) -> dict:
        return {"sha": self.default_head}

    def list_issues(self, repo: str, *, state: str = "all") -> list[dict]:
        import hashlib

        config_hash = hashlib.sha256(PROJECT_CONFIG.encode()).hexdigest()
        return [
            {
                "number": 99,
                "body": render_repository_probe(
                    probe_id="probe-1",
                    default_head=self.attested_head,
                    project_config_hash=config_hash,
                ),
            }
        ]

    def list_comments(self, repo: str, issue_number: int) -> list[dict]:
        if issue_number == 44:
            return [
                {"body": body, "user": {"login": "qiaoz", "type": "User"}}
                for body in self.comments
            ]
        import hashlib

        config_hash = hashlib.sha256(PROJECT_CONFIG.encode()).hexdigest()
        return [
            {
                "user": {"login": "worker-app[bot]", "type": "Bot"},
                "performed_via_github_app": (
                    None
                    if self.attestation_app_id is None
                    else {"id": self.attestation_app_id, "slug": "worker-app"}
                ),
                "body": render_repository_attestation(
                    probe_id="probe-1",
                    worker_id="mac-mini",
                    default_head=self.attested_head,
                    project_config_hash=config_hash,
                    attested_at="2026-07-14T00:00:00+00:00",
                ),
            }
        ]

    def get_authenticated_user(self) -> dict:
        return {"login": self.authenticated_login}

    def collaborator_permission(self, repo: str, username: str) -> str:
        return "admin"

    def mark_pull_request_ready(self, repo: str, pr_number: int) -> dict:
        self.writes.append("ready")
        self.ready_calls += 1
        self.pull["draft"] = False
        return {"number": pr_number, "isDraft": False}

    def list_reviews(self, repo: str, pr_number: int) -> list[dict]:
        return deepcopy(self.reviews)

    def create_pull_review(
        self, repo: str, pr_number: int, *, body: str, event: str = "APPROVE"
    ) -> dict:
        self.writes.append("approve")
        review = {
            "state": "APPROVED",
            "commit_id": self.pull["head"]["sha"],
            "user": {"login": "qiaoz"},
        }
        self.reviews.append(review)
        return review

    def merge_pull_request(
        self, repo: str, pr_number: int, *, expected_head: str
    ) -> dict:
        self.writes.append("merge")
        self.merge_payload = {"merge_method": "squash", "sha": expected_head}
        self.pull["merged_at"] = "2026-07-14T01:00:00Z"
        self.pull["merge_commit_sha"] = "e" * 40
        return {"merged": True, "sha": "e" * 40}

    def add_comment(self, repo: str, issue_number: int, body: str) -> dict:
        self.writes.append("comment")
        self.comments.append(body)
        return {"id": len(self.comments)}


def github_with_delivery_risk(risk: str) -> ReviewGitHub:
    github = ReviewGitHub.happy_path()
    github.pull["body"] = github.pull["body"].replace(
        "risks: []", f"risks:\n- {risk}"
    )
    return github


def reviewed_fingerprint(github: ReviewGitHub) -> str:
    from codex_mac_worker.assisted_merge import review_task

    return review_task(
        github, IssueReference("owner/repo", 12)
    ).approval_fingerprint


def test_review_snapshot_binds_issue_pr_checks_paths_and_threads() -> None:
    from codex_mac_worker.assisted_merge import review_task

    snapshot = review_task(ReviewGitHub.happy_path(), IssueReference("owner/repo", 12))

    assert snapshot.pr_number == 44
    assert snapshot.head_sha == "c" * 40
    assert snapshot.task_hash == parse_task_body(task_body()).task_hash
    assert snapshot.gates.allowed is True
    assert len(snapshot.approval_fingerprint) == 64
    assert snapshot.model == "gpt-test"


def test_review_allows_attested_bot_when_pull_app_metadata_is_absent() -> None:
    from codex_mac_worker.assisted_merge import review_task

    github = ReviewGitHub.happy_path()
    github.pull["performed_via_github_app"] = None

    snapshot = review_task(github, IssueReference("owner/repo", 12))

    assert snapshot.gates.allowed is True


@pytest.mark.parametrize(
    "metadata",
    [
        "malformed",
        [],
        777,
        {},
        {"slug": "worker-app"},
        {"id": "777", "slug": "worker-app"},
    ],
)
def test_review_blocks_malformed_pull_app_metadata(metadata: object) -> None:
    from codex_mac_worker.assisted_merge import review_task

    github = ReviewGitHub.happy_path()
    github.pull["performed_via_github_app"] = metadata

    snapshot = review_task(github, IssueReference("owner/repo", 12))

    assert snapshot.gates.allowed is False
    assert any("GitHub App" in item for item in snapshot.gates.blockers)


@pytest.mark.parametrize(
    ("user", "blocker"),
    [
        ({"login": "worker-app[bot]", "type": "User"}, "Bot"),
        ({"login": "other-worker[bot]", "type": "Bot"}, "identity"),
    ],
)
def test_review_blocks_unattested_pull_author(user: dict[str, str], blocker: str) -> None:
    from codex_mac_worker.assisted_merge import review_task

    github = ReviewGitHub.happy_path()
    github.pull["user"] = user
    github.pull["performed_via_github_app"] = None

    snapshot = review_task(github, IssueReference("owner/repo", 12))

    assert snapshot.gates.allowed is False
    assert any(blocker.lower() in item.lower() for item in snapshot.gates.blockers)


@pytest.mark.parametrize(
    "risk",
    [
        "Production build has an existing bundle-size warning",
        "生产构建存在既有的大分块体积警告",
    ],
)
def test_review_allows_benign_production_build_risk(risk: str) -> None:
    from codex_mac_worker.assisted_merge import review_task

    snapshot = review_task(
        github_with_delivery_risk(risk), IssueReference("owner/repo", 12)
    )

    assert snapshot.gates.allowed is True


@pytest.mark.parametrize(
    "risk",
    [
        "Production data may be modified",
        "Production database migration is required",
        "Production environment deployment is required",
        "需要修改生产数据",
        "需要迁移生产数据库",
        "需要部署到生产环境",
        "密码可能泄露",
        "生产 数据可能被修改",
        "生产-数据库可能被修改",
    ],
)
def test_review_blocks_explicit_production_operation_risk(risk: str) -> None:
    from codex_mac_worker.assisted_merge import review_task

    snapshot = review_task(
        github_with_delivery_risk(risk), IssueReference("owner/repo", 12)
    )

    assert snapshot.gates.allowed is False
    assert any("risks" in item for item in snapshot.gates.blockers)


def test_review_keeps_attested_worker_identity_after_default_branch_advances() -> None:
    from codex_mac_worker.assisted_merge import review_task

    github = ReviewGitHub.happy_path()
    github.default_head = "d" * 40

    snapshot = review_task(github, IssueReference("owner/repo", 12))

    assert snapshot.gates.allowed is True


@pytest.mark.parametrize(
    ("mutation", "blocker"),
    [
        ("failed_check", "checks"),
        ("pending_check", "checks"),
        ("unresolved_thread", "review threads"),
        ("conflict", "mergeable"),
        ("outside_path", "allowed_paths"),
        ("protected_path", "protected"),
        ("high_risk", "risk"),
        ("task_hash_drift", "task hash"),
        ("delivery_sha_drift", "delivery commit"),
        ("non_worker_branch", "codex/"),
        ("ruleset_drift", "Ruleset"),
        ("missing_required_check", "required checks"),
        ("wrong_issue_state", "Issue"),
        ("credential_risk", "risks"),
        ("different_app", "GitHub App"),
        ("alternate_app", "trusted Worker GitHub App"),
        ("nullable_app_metadata", "trusted Worker GitHub App"),
    ],
)
def test_review_blocks_each_unsafe_state(mutation: str, blocker: str) -> None:
    from codex_mac_worker.assisted_merge import review_task

    snapshot = review_task(
        ReviewGitHub.with_mutation(mutation), IssueReference("owner/repo", 12)
    )

    assert snapshot.gates.allowed is False
    assert any(blocker.lower() in item.lower() for item in snapshot.gates.blockers)


def test_merge_rechecks_head_and_writes_nothing_after_drift(tmp_path: Path) -> None:
    from codex_mac_worker.assisted_merge import MergeBlocked, merge_task
    from codex_mac_worker.control_state import ControlState

    github = ReviewGitHub.happy_path()
    github.pull["head"]["sha"] = "d" * 40
    state = ControlState(tmp_path / "state.db")
    with pytest.raises(MergeBlocked, match="approval expired"):
        merge_task(
            github,
            state,
            IssueReference("owner/repo", 12),
            expected_head="c" * 40,
            expected_fingerprint="f" * 64,
        )
    state.close()

    assert github.writes == []


def test_merge_approves_squashes_and_records_audit(tmp_path: Path) -> None:
    from codex_mac_worker.assisted_merge import merge_task
    from codex_mac_worker.control_state import ControlState

    github = ReviewGitHub.happy_path()
    fingerprint = reviewed_fingerprint(github)
    state = ControlState(tmp_path / "state.db")
    result = merge_task(
        github,
        state,
        IssueReference("owner/repo", 12),
        expected_head="c" * 40,
        expected_fingerprint=fingerprint,
    )
    state.close()

    assert result.merged is True
    assert github.ready_calls == 1
    assert github.merge_payload == {"merge_method": "squash", "sha": "c" * 40}
    assert "<!-- codex-human-approval:v1 -->" in github.comments[-1]


def test_merge_is_idempotent_after_confirmed_success(tmp_path: Path) -> None:
    from codex_mac_worker.assisted_merge import merge_task
    from codex_mac_worker.control_state import ControlState

    github = ReviewGitHub.happy_path()
    fingerprint = reviewed_fingerprint(github)
    state = ControlState(tmp_path / "state.db")
    first = merge_task(
        github, state, IssueReference("owner/repo", 12), expected_head="c" * 40,
        expected_fingerprint=fingerprint,
    )
    second = merge_task(
        github, state, IssueReference("owner/repo", 12), expected_head="c" * 40,
        expected_fingerprint=fingerprint,
    )
    state.close()

    assert first == second
    assert github.writes.count("merge") == 1


def test_merge_reconciles_uncertain_response_without_retrying(tmp_path: Path) -> None:
    from codex_mac_worker.assisted_merge import merge_task
    from codex_mac_worker.control_state import ControlState

    class UncertainGitHub(ReviewGitHub):
        def merge_pull_request(
            self, repo: str, pr_number: int, *, expected_head: str
        ) -> dict:
            super().merge_pull_request(repo, pr_number, expected_head=expected_head)
            raise RuntimeError("connection dropped after GitHub accepted merge")

    github = UncertainGitHub()
    fingerprint = reviewed_fingerprint(github)
    state = ControlState(tmp_path / "state.db")
    result = merge_task(
        github, state, IssueReference("owner/repo", 12), expected_head="c" * 40,
        expected_fingerprint=fingerprint,
    )
    state.close()

    assert result.merged is True
    assert result.merge_commit_sha == "e" * 40
    assert github.writes.count("merge") == 1


def test_merge_recovers_after_audit_comment_failure(tmp_path: Path) -> None:
    from codex_mac_worker.assisted_merge import merge_task
    from codex_mac_worker.control_state import ControlState

    class AuditFailureGitHub(ReviewGitHub):
        def __init__(self) -> None:
            super().__init__()
            self.fail_audit_once = True

        def add_comment(self, repo: str, issue_number: int, body: str) -> dict:
            if issue_number == 44 and self.fail_audit_once:
                self.fail_audit_once = False
                raise RuntimeError("comment connection failed")
            return super().add_comment(repo, issue_number, body)

    github = AuditFailureGitHub()
    fingerprint = reviewed_fingerprint(github)
    state = ControlState(tmp_path / "state.db")
    with pytest.raises(RuntimeError, match="comment connection failed"):
        merge_task(
            github, state, IssueReference("owner/repo", 12), expected_head="c" * 40,
            expected_fingerprint=fingerprint,
        )

    github.authenticated_login = "maintainer-2"
    result = merge_task(
        github, state, IssueReference("owner/repo", 12), expected_head="c" * 40,
        expected_fingerprint=fingerprint,
    )
    state.close()

    assert result.merged is True
    assert result.actor_login == "qiaoz"
    assert github.writes.count("merge") == 1
    assert len(github.comments) == 1


def test_merge_rejects_stale_fingerprint_before_any_write(tmp_path: Path) -> None:
    from codex_mac_worker.assisted_merge import MergeBlocked, merge_task
    from codex_mac_worker.control_state import ControlState

    github = ReviewGitHub.happy_path()
    state = ControlState(tmp_path / "state.db")
    with pytest.raises(MergeBlocked, match="fingerprint"):
        merge_task(
            github,
            state,
            IssueReference("owner/repo", 12),
            expected_head="c" * 40,
            expected_fingerprint="f" * 64,
        )
    state.close()

    assert github.writes == []
