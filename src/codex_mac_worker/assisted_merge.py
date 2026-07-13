from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable

from .config import parse_project_config
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
from .repository_onboarding import RULESET_NAME, _ruleset_valid


class AssistedMergeError(RuntimeError):
    """Raised when a Worker delivery cannot be reviewed safely."""


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
    gates: GateResult
    approval_fingerprint: str


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
    return matches[0]


def _authoritative_worker_login(github: Any, repo: str) -> str | None:
    repository = github.get_repository(repo)
    default_branch = str(repository.get("default_branch", ""))
    default_head = str(github.get_commit(repo, default_branch).get("sha", "")).lower()
    project_text = github.get_repository_file(
        repo, ".codex-worker/project.toml", ref=default_head
    )
    project_hash = hashlib.sha256(project_text.encode("utf-8")).hexdigest()
    for issue in reversed(github.list_issues(repo, state="all")):
        body = str(issue.get("body", ""))
        if REPOSITORY_PROBE_MARKER not in body:
            continue
        try:
            probe = parse_repository_probe(body)
        except ProtocolError:
            continue
        if probe.default_head != default_head or probe.project_config_hash != project_hash:
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
                and attestation.default_head == default_head
                and attestation.project_config_hash == project_hash
            ):
                login = str(user.get("login", ""))
                return login or None
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
    if not _ruleset_valid(current):
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
    for item in github.list_check_runs(repo, head_sha):
        name = str(item.get("name", ""))
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
        state = str(item.get("state") or "")
        checks.append({"name": name, "status": "completed" if state == "success" else state, "conclusion": state})
        if state != "success":
            blockers.append(f"checks: legacy status {name or 'unnamed status'} is {state or 'pending'}")
    return tuple(checks)


def review_task(github: Any, reference: IssueReference) -> ReviewSnapshot:
    issue = github.get_issue(reference.repo, reference.number)
    spec = parse_task_body(str(issue.get("body", "")))
    pull, delivery = _matching_delivery(github, reference)
    blockers: list[str] = []

    base = pull.get("base", {})
    head = pull.get("head", {})
    base_branch = str(base.get("ref", ""))
    base_sha = str(base.get("sha", "")).lower()
    head_branch = str(head.get("ref", ""))
    head_sha = str(head.get("sha", "")).lower()
    pr_number = int(pull["number"])

    if not head_branch.startswith("codex/"):
        blockers.append("source branch must begin with codex/")
    worker_login = _authoritative_worker_login(github, reference.repo)
    author_login = str(pull.get("user", {}).get("login", ""))
    if worker_login is None:
        blockers.append("Worker identity has no current readiness attestation")
    elif author_login != worker_login:
        blockers.append("PR author does not match the attested Worker identity")
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
        gates=gates,
        approval_fingerprint=fingerprint,
    )
