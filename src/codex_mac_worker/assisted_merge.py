from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any, Iterable

import yaml

from .config import parse_project_config
from .control_state import ControlState, operation_id
from .merge_policy import RULESET_NAME, classify_ruleset
from .policy import PolicyError, validate_changed_paths, validate_task_policy
from .protocol import (
    DELIVERY_MARKER,
    REPOSITORY_ATTESTATION_MARKER,
    REPOSITORY_PROBE_MARKER,
    DeliveryMetadata,
    ProtocolError,
    parse_delivery_block,
    parse_repository_attestation,
    parse_repository_probe,
    parse_task_body,
)
from .references import IssueReference


class AssistedMergeError(RuntimeError):
    """Raised when a Worker delivery cannot be reviewed safely."""


class MergeBlocked(AssistedMergeError):
    """Raised before an unsafe or stale merge can be attempted."""


@dataclass(frozen=True, slots=True)
class GateResult:
    allowed: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    repo: str
    issue_number: int
    pr_number: int
    pr_url: str
    base_branch: str
    base_sha: str
    head_sha: str
    is_draft: bool
    task_hash: str
    context_commit: str
    changed_paths: tuple[str, ...]
    additions: int
    deletions: int
    checks: tuple[dict[str, str], ...]
    acceptance_results: tuple[dict[str, str], ...]
    model: str | None
    cli_version: str | None
    risks: tuple[str, ...]
    needs_human: tuple[str, ...]
    unresolved_threads: tuple[str, ...]
    ruleset_profile: str | None
    gates: GateResult
    approval_fingerprint: str


@dataclass(frozen=True, slots=True)
class MergeResult:
    repo: str
    issue_number: int
    pr_number: int
    approved_head: str
    merge_commit_sha: str
    actor_login: str
    approval_fingerprint: str
    merged: bool


APPROVAL_MARKER = "<!-- codex-human-approval:v1 -->"
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_UNSAFE_RISK_RE = re.compile(
    r"\b(high[-\s]?risk|credentials?|secrets?|passwords?|"
    r"deploy(?:ment|ed|ing)?|migrations?|irreversible|"
    r"prod(?:uction)?[\s_-]+(?:data|databases?|environments?))\b|"
    r"高风险|凭据|密钥|密码|部署|迁移|不可逆|生产[\s_-]*(?:数据|数据库|环境)",
    re.IGNORECASE,
)


def evaluate_merge_gates(blockers: Iterable[str]) -> GateResult:
    unique = tuple(dict.fromkeys(item for item in blockers if item))
    return GateResult(allowed=not unique, blockers=unique)


def approval_fingerprint(
    *,
    repo: str,
    issue_number: int,
    pr_number: int,
    task_hash: str,
    context_commit: str,
    base_sha: str,
    head_sha: str,
) -> str:
    payload = {
        "schema_version": 1,
        "repo": repo,
        "issue_number": issue_number,
        "pr_number": pr_number,
        "task_hash": task_hash,
        "context_commit": context_commit,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _matching_delivery(github: Any, reference: IssueReference) -> tuple[dict[str, Any], DeliveryMetadata]:
    matches: list[tuple[dict[str, Any], DeliveryMetadata]] = []
    for pull in github.list_pull_requests(reference.repo, state="open"):
        body = str(pull.get("body", ""))
        if DELIVERY_MARKER not in body:
            continue
        try:
            delivery = parse_delivery_block(body)
        except ProtocolError:
            continue
        if delivery.issue_number == reference.number:
            matches.append((pull, delivery))
    if len(matches) != 1:
        raise AssistedMergeError(
            f"expected exactly one open Worker PR for Issue #{reference.number}; found {len(matches)}"
        )
    summary, _ = matches[0]
    pull = github.get_pull_request(reference.repo, int(summary["number"]))
    delivery = parse_delivery_block(str(pull.get("body", "")))
    if delivery.issue_number != reference.number:
        raise AssistedMergeError("full PR delivery metadata changed during review")
    return pull, delivery


def _authoritative_worker_identity(
    github: Any, repo: str
) -> tuple[str, int] | None:
    repository = github.get_repository(repo)
    default_branch = str(repository.get("default_branch", ""))
    default_head = str(github.get_commit(repo, default_branch).get("sha", "")).lower()
    project_text = github.get_repository_file(
        repo, ".codex-worker/project.toml", ref=default_head
    )
    project = parse_project_config(project_text)
    project_hash = hashlib.sha256(project_text.encode("utf-8")).hexdigest()
    for issue in reversed(github.list_issues(repo, state="all")):
        body = str(issue.get("body", ""))
        if REPOSITORY_PROBE_MARKER not in body:
            continue
        try:
            probe = parse_repository_probe(body)
        except ProtocolError:
            continue
        if probe.project_config_hash != project_hash:
            continue
        for comment in reversed(github.list_comments(repo, int(issue["number"]))):
            comment_body = str(comment.get("body", ""))
            user = comment.get("user", {})
            if (
                REPOSITORY_ATTESTATION_MARKER not in comment_body
                or user.get("type") != "Bot"
            ):
                continue
            try:
                attestation = parse_repository_attestation(comment_body)
            except ProtocolError:
                continue
            if (
                attestation.probe_id == probe.probe_id
                and attestation.default_head == probe.default_head
                and attestation.project_config_hash == project_hash
                ):
                    login = str(user.get("login", ""))
                    app_metadata = comment.get("performed_via_github_app")
                    app_id = (
                        app_metadata.get("id")
                        if isinstance(app_metadata, dict)
                        else None
                    )
                    if login and app_id == project.worker_github_app_id:
                        return login, app_id
    return None


def _required_checks(ruleset: dict[str, Any]) -> set[str]:
    required: set[str] = set()
    for rule in ruleset.get("rules", []):
        if rule.get("type") != "required_status_checks":
            continue
        for item in rule.get("parameters", {}).get("required_status_checks", []):
            context = item.get("context") if isinstance(item, dict) else None
            if isinstance(context, str) and context:
                required.add(context)
    return required


def _ruleset(github: Any, repo: str, blockers: list[str]) -> dict[str, Any]:
    matching = [
        item for item in github.list_rulesets(repo) if item.get("name") == RULESET_NAME
    ]
    if len(matching) != 1:
        blockers.append("Ruleset: expected exactly one Codex Worker Ruleset")
        return {}
    current = github.get_ruleset(repo, int(matching[0]["id"]))
    if classify_ruleset(current) is None:
        blockers.append("Ruleset: Codex Worker branch protection is missing or unsafe")
    return current


def _collect_checks(
    github: Any,
    repo: str,
    head_sha: str,
    required: set[str],
    blockers: list[str],
) -> tuple[dict[str, str], ...]:
    checks: list[dict[str, str]] = []
    observed: set[str] = set()
    for item in github.list_check_runs(repo, head_sha):
        name = str(item.get("name", ""))
        observed.add(name)
        status = str(item.get("status") or "")
        conclusion = str(item.get("conclusion") or "")
        checks.append({"name": name, "status": status, "conclusion": conclusion})
        if status != "completed":
            blockers.append(f"checks: {name or 'unnamed check'} is {status or 'pending'}")
        elif conclusion == "success":
            pass
        elif conclusion == "neutral" and name not in required:
            pass
        else:
            blockers.append(
                f"checks: {name or 'unnamed check'} concluded {conclusion or 'without a result'}"
            )
    status_payload = github.get_combined_status(repo, head_sha)
    for item in status_payload.get("statuses", []):
        name = str(item.get("context", ""))
        observed.add(name)
        state = str(item.get("state") or "")
        checks.append({"name": name, "status": "completed" if state == "success" else state, "conclusion": state})
        if state != "success":
            blockers.append(f"checks: legacy status {name or 'unnamed status'} is {state or 'pending'}")
    missing = sorted(required - observed)
    if missing:
        blockers.append("required checks are missing: " + ", ".join(missing))
    return tuple(checks)


def review_task(
    github: Any,
    reference: IssueReference,
    *,
    allowed_lifecycle_labels: frozenset[str] = frozenset({"codex:awaiting-review"}),
) -> ReviewSnapshot:
    issue = github.get_issue(reference.repo, reference.number)
    spec = parse_task_body(str(issue.get("body", "")))
    pull, delivery = _matching_delivery(github, reference)
    blockers: list[str] = []
    if issue.get("state") != "open":
        blockers.append("Issue must remain open while awaiting review")
    status_labels: set[str] = set()
    for item in issue.get("labels", []):
        label = str(item.get("name", "")) if isinstance(item, dict) else str(item)
        if label.startswith("codex:"):
            status_labels.add(label)
    if len(status_labels) != 1 or not status_labels.issubset(allowed_lifecycle_labels):
        blockers.append(
            "Issue must have exactly one allowed Worker merge lifecycle label"
        )

    base = pull.get("base", {})
    head = pull.get("head", {})
    base_branch = str(base.get("ref", ""))
    base_sha = str(base.get("sha", "")).lower()
    head_branch = str(head.get("ref", ""))
    head_sha = str(head.get("sha", "")).lower()
    pr_number = int(pull["number"])

    if not head_branch.startswith("codex/"):
        blockers.append("source branch must begin with codex/")
    worker_identity = _authoritative_worker_identity(github, reference.repo)
    pull_user = pull.get("user", {})
    author_login = str(pull_user.get("login", ""))
    author_type = str(pull_user.get("type", ""))
    pull_app_metadata = pull.get("performed_via_github_app")
    pull_app_id = (
        pull_app_metadata.get("id")
        if isinstance(pull_app_metadata, dict)
        else None
    )
    if worker_identity is None:
        blockers.append(
            "Worker identity has no current attestation from the trusted Worker GitHub App"
        )
    else:
        worker_login, worker_app_id = worker_identity
        if author_type != "Bot":
            blockers.append("PR author is not a GitHub Bot")
        if author_login != worker_login:
            blockers.append("PR author does not match the attested Worker identity")
        if pull_app_metadata is not None and (
            not isinstance(pull_app_metadata, dict)
            or pull_app_id != worker_app_id
        ):
            blockers.append("PR was not created by the attested Worker GitHub App")
    if delivery.task_hash != spec.task_hash:
        blockers.append("delivery task hash differs from the frozen Issue task hash")
    if delivery.context_commit != spec.context_commit:
        blockers.append("delivery context commit differs from the frozen Issue")
    if delivery.delivery_commit != head_sha:
        blockers.append("delivery commit differs from the current PR head")
    if delivery.verification_profile != spec.verification_profile:
        blockers.append("delivery verification profile differs from the frozen Issue")
    if not delivery.verification_passed:
        blockers.append("Worker repository verification did not pass")

    config_text = github.get_repository_file(
        reference.repo, ".codex-worker/project.toml", ref=base_sha
    )
    project = parse_project_config(config_text)
    try:
        validate_task_policy(spec, project)
    except PolicyError as exc:
        blockers.append(str(exc))

    files = github.list_pull_files(reference.repo, pr_number)
    paths: list[str] = []
    for item in files:
        paths.append(str(item.get("filename", "")))
        if item.get("status") == "renamed" and item.get("previous_filename"):
            paths.append(str(item["previous_filename"]))
    additions = sum(int(item.get("additions", 0)) for item in files)
    deletions = sum(int(item.get("deletions", 0)) for item in files)
    try:
        validate_changed_paths(spec, project, paths, additions + deletions)
    except PolicyError as exc:
        blockers.append(str(exc))

    if pull.get("mergeable") is not True:
        blockers.append("PR is not reported mergeable by GitHub")

    ruleset = _ruleset(github, reference.repo, blockers)
    ruleset_profile = classify_ruleset(ruleset)
    checks = _collect_checks(
        github,
        reference.repo,
        head_sha,
        _required_checks(ruleset),
        blockers,
    )
    unresolved_threads: list[str] = []
    for thread in github.list_review_threads(reference.repo, pr_number):
        if thread.get("isResolved") is True:
            continue
        nodes = thread.get("comments", {}).get("nodes", [])
        url = str(nodes[0].get("url", "")) if nodes else ""
        unresolved_threads.append(url or "unresolved review thread")
    if unresolved_threads:
        blockers.append(f"review threads: {len(unresolved_threads)} unresolved")

    if len(delivery.acceptance_results) != len(spec.acceptance):
        blockers.append("acceptance evidence does not match the frozen criteria")
    else:
        for criterion, result in zip(
            spec.acceptance, delivery.acceptance_results, strict=True
        ):
            if result.get("criterion") != criterion:
                blockers.append("acceptance evidence does not match the frozen criteria")
            if result.get("status") != "met":
                blockers.append(f"acceptance criterion still needs review: {criterion}")
    if delivery.needs_human:
        blockers.append("human dependencies remain unresolved")
    if any(_UNSAFE_RISK_RE.search(item) for item in delivery.risks):
        blockers.append("delivery risks mention high-risk or operational work")

    fingerprint = approval_fingerprint(
        repo=reference.repo,
        issue_number=reference.number,
        pr_number=pr_number,
        task_hash=spec.task_hash,
        context_commit=spec.context_commit,
        base_sha=base_sha,
        head_sha=head_sha,
    )
    gates = evaluate_merge_gates(blockers)
    return ReviewSnapshot(
        repo=reference.repo,
        issue_number=reference.number,
        pr_number=pr_number,
        pr_url=str(pull.get("html_url", "")),
        base_branch=base_branch,
        base_sha=base_sha,
        head_sha=head_sha,
        is_draft=bool(pull.get("draft")),
        task_hash=spec.task_hash,
        context_commit=spec.context_commit,
        changed_paths=tuple(paths),
        additions=additions,
        deletions=deletions,
        checks=checks,
        acceptance_results=delivery.acceptance_results,
        model=delivery.model,
        cli_version=delivery.cli_version,
        risks=delivery.risks,
        needs_human=delivery.needs_human,
        unresolved_threads=tuple(unresolved_threads),
        ruleset_profile=ruleset_profile,
        gates=gates,
        approval_fingerprint=fingerprint,
    )


def render_approval_audit(
    *, snapshot: ReviewSnapshot, actor_login: str, approved_at: str
) -> str:
    return _render_approval_context(
        _approval_context(snapshot, actor_login), approved_at=approved_at
    )


def _approval_context(
    snapshot: ReviewSnapshot,
    actor_login: str,
) -> dict[str, Any]:
    return {
        "repo": snapshot.repo,
        "issue_number": snapshot.issue_number,
        "pr_number": snapshot.pr_number,
        "task_hash": snapshot.task_hash,
        "approved_head": snapshot.head_sha,
        "approval_fingerprint": snapshot.approval_fingerprint,
        "actor_login": actor_login,
    }


def _render_approval_context(context: dict[str, Any], *, approved_at: str) -> str:
    payload = {
        "schema_version": 1,
        "approval_fingerprint": context["approval_fingerprint"],
        "actor_login": context["actor_login"],
        "issue_number": context["issue_number"],
        "pr_number": context["pr_number"],
        "task_hash": context["task_hash"],
        "approved_head": context["approved_head"],
        "approved_at": approved_at,
    }
    machine = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip()
    return f"{APPROVAL_MARKER}\n```yaml\n{machine}\n```\n"


def _delivery_pulls(github: Any, reference: IssueReference) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for pull in github.list_pull_requests(reference.repo, state="all"):
        body = str(pull.get("body", ""))
        if DELIVERY_MARKER not in body:
            continue
        try:
            delivery = parse_delivery_block(body)
        except ProtocolError:
            continue
        if delivery.issue_number == reference.number:
            matches.append(
                github.get_pull_request(reference.repo, int(pull["number"]))
            )
    return matches


def _merge_result(raw: dict[str, Any]) -> MergeResult:
    return MergeResult(
        repo=str(raw["repo"]),
        issue_number=int(raw["issue_number"]),
        pr_number=int(raw["pr_number"]),
        approved_head=str(raw["approved_head"]),
        merge_commit_sha=str(raw["merge_commit_sha"]),
        actor_login=str(raw["actor_login"]),
        approval_fingerprint=str(raw["approval_fingerprint"]),
        merged=bool(raw["merged"]),
    )


def _audit_once(github: Any, context: dict[str, Any]) -> None:
    expected = (
        APPROVAL_MARKER in str(comment.get("body", ""))
        and str(context["approval_fingerprint"]) in str(comment.get("body", ""))
        for comment in github.list_comments(
            str(context["repo"]), int(context["pr_number"])
        )
    )
    if any(expected):
        return
    github.add_comment(
        str(context["repo"]),
        int(context["pr_number"]),
        _render_approval_context(
            context,
            approved_at=datetime.now(UTC).isoformat(),
        ),
    )


def merge_task(
    github: Any,
    state: ControlState,
    reference: IssueReference,
    *,
    expected_head: str,
    expected_fingerprint: str,
) -> MergeResult:
    expected_head = expected_head.lower()
    expected_fingerprint = expected_fingerprint.lower()
    if not _FULL_SHA_RE.fullmatch(expected_head):
        raise MergeBlocked("expected_head must be a full 40-character Git SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint):
        raise MergeBlocked("expected_fingerprint must be a full 64-character hash")

    candidates = _delivery_pulls(github, reference)
    if len(candidates) != 1:
        raise MergeBlocked(
            f"expected exactly one Worker PR for Issue #{reference.number}; found {len(candidates)}"
        )
    candidate = candidates[0]
    pr_number = int(candidate["number"])
    current_head = str(candidate.get("head", {}).get("sha", "")).lower()
    key = operation_id(
        "task-merge",
        f"{reference.repo}#{pr_number}:{expected_fingerprint}",
        expected_head,
    )
    existing = state.get(key)
    merged_at = candidate.get("merged_at")
    if existing is not None and existing["state"] == "completed":
        if not merged_at or current_head != expected_head:
            raise MergeBlocked("completed merge ledger does not match GitHub state")
        completed = _merge_result(existing["result"])
        if completed.approval_fingerprint != expected_fingerprint:
            raise MergeBlocked("completed merge fingerprint does not match approval")
        return completed
    if current_head != expected_head:
        raise MergeBlocked("approval expired because the PR head changed")
    if merged_at and existing is None:
        raise MergeBlocked("PR was already merged outside this approval operation")
    if merged_at and existing is not None:
        user = github.get_authenticated_user()
        current_login = str(user.get("login", ""))
        permission = github.collaborator_permission(reference.repo, current_login)
        context = existing.get("result")
        if not isinstance(context, dict):
            raise MergeBlocked("approved merge context is missing from the ledger")
        actor_login = str(context.get("actor_login", ""))
        approved = any(
            str(review.get("state", "")).upper() == "APPROVED"
            and str(review.get("user", {}).get("login", "")) == actor_login
            and str(review.get("commit_id", "")).lower() == expected_head
            for review in github.list_reviews(reference.repo, pr_number)
        )
        if permission not in {"admin", "maintain"} or not approved:
            raise MergeBlocked("unable to reconcile the original approved merge actor")
        if (
            context.get("repo") != reference.repo
            or int(context.get("issue_number", 0)) != reference.number
            or int(context.get("pr_number", 0)) != pr_number
            or context.get("approved_head") != expected_head
            or context.get("approval_fingerprint") != expected_fingerprint
        ):
            raise MergeBlocked("approval fingerprint no longer matches the merged PR")
        result = MergeResult(
            repo=reference.repo,
            issue_number=reference.number,
            pr_number=pr_number,
            approved_head=expected_head,
            merge_commit_sha=str(candidate.get("merge_commit_sha", "")),
            actor_login=actor_login,
            approval_fingerprint=expected_fingerprint,
            merged=True,
        )
        _audit_once(github, context)
        state.complete(key, asdict(result))
        return result

    snapshot = review_task(github, reference)
    if snapshot.head_sha != expected_head:
        raise MergeBlocked("approval expired because the PR head changed")
    if snapshot.approval_fingerprint != expected_fingerprint:
        raise MergeBlocked("approval fingerprint no longer matches the reviewed task")
    if not snapshot.gates.allowed:
        raise MergeBlocked("merge gates blocked: " + "; ".join(snapshot.gates.blockers))

    user = github.get_authenticated_user()
    actor_login = str(user.get("login", ""))
    permission = github.collaborator_permission(reference.repo, actor_login)
    if not actor_login or permission not in {"admin", "maintain"}:
        raise MergeBlocked("authenticated user must have admin or maintain permission")
    if str(candidate.get("user", {}).get("login", "")) == actor_login:
        raise MergeBlocked("normal Worker PRs cannot be approved by their own author")

    state.begin(
        key,
        "task-merge",
        f"{reference.repo}#{pr_number}:{expected_fingerprint}",
        expected_head,
    )
    approval_context = _approval_context(snapshot, actor_login)
    state.record_context(key, approval_context)

    if snapshot.is_draft:
        github.mark_pull_request_ready(reference.repo, pr_number)
        snapshot = review_task(github, reference)
        if snapshot.head_sha != expected_head:
            raise MergeBlocked("approval expired because the PR head changed after Ready")
        if snapshot.approval_fingerprint != expected_fingerprint:
            raise MergeBlocked("approval fingerprint changed after Ready")
        if not snapshot.gates.allowed:
            raise MergeBlocked(
                "merge gates blocked after Ready: " + "; ".join(snapshot.gates.blockers)
            )

    current_approval = any(
        str(review.get("state", "")).upper() == "APPROVED"
        and str(review.get("user", {}).get("login", "")) == actor_login
        and str(review.get("commit_id", "")).lower() == expected_head
        for review in github.list_reviews(reference.repo, pr_number)
    )
    if not current_approval:
        github.create_pull_review(
            reference.repo,
            pr_number,
            body=f"Approved by codexctl for immutable head `{expected_head}`.",
            event="APPROVE",
        )

    try:
        merge_payload = github.merge_pull_request(
            reference.repo,
            pr_number,
            expected_head=expected_head,
        )
        if merge_payload.get("merged") is not True:
            raise MergeBlocked(str(merge_payload.get("message", "GitHub rejected the merge")))
        merge_commit_sha = str(merge_payload.get("sha", ""))
    except MergeBlocked:
        raise
    except Exception:
        reconciled = github.get_pull_request(reference.repo, pr_number)
        reconciled_head = str(reconciled.get("head", {}).get("sha", "")).lower()
        if not reconciled.get("merged_at") or reconciled_head != expected_head:
            raise
        merge_commit_sha = str(reconciled.get("merge_commit_sha", ""))

    result = MergeResult(
        repo=reference.repo,
        issue_number=reference.number,
        pr_number=pr_number,
        approved_head=expected_head,
        merge_commit_sha=merge_commit_sha,
        actor_login=actor_login,
        approval_fingerprint=snapshot.approval_fingerprint,
        merged=True,
    )
    _audit_once(github, approval_context)
    state.complete(key, asdict(result))
    return result
