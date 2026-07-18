from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Protocol

import yaml

from .automatic_merge import automatic_merge_task
from .config import (
    ProjectConfig,
    RepositoryConfig,
    WorkerConfig,
    load_project_config,
    parse_project_config,
)
from .coordination import active_task_conflicts
from .gitops import GitOperations
from .policy import PolicyError, validate_changed_paths, validate_task_policy
from .prompting import build_execution_prompt, build_revision_prompt, result_schema
from .protocol import (
    DeliveryMetadata,
    ProtocolError,
    parse_command_comment,
    parse_repository_probe,
    parse_task_body,
    render_delivery_block,
    render_repository_attestation,
)
from .references import IssueReference
from .runner import CodexRunner, RunnerResult, RunnerTimeout
from .store import EventStore
from .verification import (
    CommandResult,
    VerificationResult,
    run_commands,
    run_verification,
    scan_for_secrets,
)


STATUS_PREFIX = "codex:"
TERMINAL_STATES = {"awaiting-review", "needs-attention", "completed", "cancelled"}
AUTHORIZED_PERMISSIONS = {"admin", "maintain", "write"}
DELIVERY_RETRY_TIMEOUT_SECONDS = 1800


class GitHubPort(Protocol):
    def get_issue(self, repo: str, issue_number: int) -> dict[str, Any]: ...
    def set_labels(self, repo: str, issue_number: int, labels: list[str]) -> dict[str, Any]: ...
    def add_comment(self, repo: str, issue_number: int, body: str) -> dict[str, Any]: ...
    def update_comment(self, repo: str, comment_id: int, body: str) -> dict[str, Any]: ...
    def list_comments(self, repo: str, issue_number: int) -> list[dict[str, Any]]: ...
    def collaborator_permission(self, repo: str, username: str) -> str: ...
    def create_draft_pr(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> dict[str, Any]: ...
    def update_pull_request(
        self, repo: str, pr_number: int, *, body: str
    ) -> dict[str, Any]: ...
    def get_repository(self, repo: str) -> dict[str, Any]: ...
    def get_commit(self, repo: str, ref: str) -> dict[str, Any]: ...
    def get_repository_file(self, repo: str, path: str, *, ref: str) -> str: ...
    def update_issue(
        self,
        repo: str,
        issue_number: int,
        *,
        labels: list[str] | None = None,
        state: str | None = None,
    ) -> dict[str, Any]: ...


class RunnerPort(Protocol):
    def run(self, worktree: Path, prompt: str, output_schema: Path, **kwargs: Any) -> RunnerResult: ...


def iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", title.lower()).strip("-")
    return slug[:48] or "task"


class WorkerService:
    def __init__(
        self,
        *,
        config: WorkerConfig,
        github: GitHubPort,
        token_provider: Callable[[], str],
        store: EventStore,
        git: GitOperations,
        runner: RunnerPort,
    ) -> None:
        self.config = config
        self.github = github
        self.token_provider = token_provider
        self.store = store
        self.git = git
        self.runner = runner
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True
        stop_runner = getattr(self.runner, "stop_current", None)
        if stop_runner is not None:
            stop_runner()

    def _labels(self, issue: dict[str, Any]) -> list[str]:
        labels: list[str] = []
        for item in issue.get("labels", []):
            if isinstance(item, dict):
                value = item.get("name")
            else:
                value = item
            if isinstance(value, str):
                labels.append(value)
        return labels

    def _set_state(self, repo: str, issue: dict[str, Any], state: str) -> None:
        labels = [label for label in self._labels(issue) if not label.startswith(STATUS_PREFIX)]
        labels.append(f"codex:{state}")
        self.github.set_labels(repo, int(issue["number"]), labels)

    def _status_body(
        self,
        *,
        task_hash: str,
        state: str,
        branch: str,
        progress_at: str,
        detail: str = "",
        hard_deadline_at: str = "",
    ) -> str:
        payload = {
            "schema_version": 1,
            "worker_id": self.config.worker_id,
            "task_hash": task_hash,
            "state": state,
            "branch": branch,
            "heartbeat_at": iso_now(),
            "progress_at": progress_at,
            "hard_deadline_at": hard_deadline_at,
            "detail": detail,
        }
        return (
            "<!-- codex-worker-status:v1 -->\n```yaml\n"
            + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip()
            + "\n```\n"
        )

    def _mark_attention(
        self,
        repo: str,
        issue: dict[str, Any],
        task_hash: str,
        branch: str,
        detail: str,
        *,
        allow_remote: bool = True,
    ) -> None:
        number = int(issue["number"])
        self.store.upsert_task(
            repo=repo,
            issue_number=number,
            task_hash=task_hash,
            state="needs-attention",
            branch=branch or None,
        )
        if allow_remote:
            self._set_state(repo, issue, "needs-attention")
            self.github.add_comment(
                repo,
                number,
                self._status_body(
                    task_hash=task_hash,
                    state="needs-attention",
                    branch=branch,
                    progress_at=iso_now(),
                    detail=detail[:4000],
                ),
            )

    def _validate_context_files(self, worktree: Path, paths: tuple[str, ...]) -> None:
        root = worktree.resolve()
        for relative in paths:
            candidate = (worktree / relative).resolve()
            if not candidate.is_relative_to(root) or not candidate.is_file():
                raise PolicyError(f"context file is missing or outside worktree: {relative}")

    def _validate_runtime_policy(self, worktree: Path) -> None:
        if (worktree / ".codex" / "config.toml").exists():
            raise PolicyError(
                "project Codex config is forbidden because it can override Worker permissions"
            )

    def _validate_project_worker_app(self, project: ProjectConfig) -> None:
        if project.worker_github_app_id != int(self.config.github_app_id):
            raise PolicyError(
                "project config is not bound to this trusted GitHub App"
            )

    def validate_repository_authority(
        self, repository: RepositoryConfig
    ) -> ProjectConfig:
        payload = self.github.get_repository(repository.name)
        default_branch = str(payload.get("default_branch", ""))
        if not default_branch:
            raise PolicyError("repository default branch is missing")
        current_head = str(
            self.github.get_commit(repository.name, default_branch).get("sha", "")
        ).lower()
        if not re.fullmatch(r"[0-9a-f]{40}", current_head):
            raise PolicyError("repository default branch head is invalid")
        project_text = self.github.get_repository_file(
            repository.name,
            ".codex-worker/project.toml",
            ref=current_head,
        )
        project = parse_project_config(project_text)
        if project.default_base_branch != default_branch:
            raise PolicyError("project config default branch does not match repository")
        self._validate_project_worker_app(project)
        return project

    def _validate_issue_author(self, repo: str, issue: dict[str, Any]) -> None:
        author = str(issue.get("user", {}).get("login", ""))
        permission = self.github.collaborator_permission(repo, author) if author else "none"
        if author not in self.config.authorized_users or permission not in AUTHORIZED_PERMISSIONS:
            raise PolicyError(f"issue author {author or '<missing>'} is not authorized")

    def _repository_attestation_state(
        self,
        *,
        repository_identity: str,
        issue_number: int,
        probe_id: str,
        default_head: str,
        project_config_hash: str,
        issue_updated_at: str,
    ) -> tuple[str, bool, str, dict[str, str]]:
        identity = json.dumps(
            [
                repository_identity,
                issue_number,
                probe_id,
                default_head,
                project_config_hash,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        state_key = "repository-attested-at:" + hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()
        existing = self.store.get_worker_state(state_key)
        if isinstance(existing, str) and existing:
            existing = {
                "attested_at": existing,
                "issue_updated_at": issue_updated_at,
            }
            self.store.set_worker_state(state_key, existing)
        if isinstance(existing, dict):
            attested_at = existing.get("attested_at")
            observed_update = existing.get("issue_updated_at")
            if isinstance(attested_at, str) and attested_at:
                retry_failed = bool(
                    issue_updated_at
                    and isinstance(observed_update, str)
                    and observed_update
                    and issue_updated_at != observed_update
                )
                retry_state = {
                    "attested_at": attested_at,
                    "issue_updated_at": issue_updated_at,
                }
                return attested_at, retry_failed, state_key, retry_state
        attested_at = iso_now()
        state = {
            "attested_at": attested_at,
            "issue_updated_at": issue_updated_at,
        }
        self.store.set_worker_state(state_key, state)
        return attested_at, False, state_key, state

    def process_repository_probe(
        self,
        repository: RepositoryConfig,
        issue: dict[str, Any],
    ) -> None:
        repo = repository.name
        number = int(issue["number"])
        try:
            self._validate_issue_author(repo, issue)
            probe = parse_repository_probe(str(issue.get("body", "")))
            repository_payload = self.github.get_repository(repo)
            default_branch = str(repository_payload.get("default_branch", ""))
            if not default_branch:
                raise PolicyError("repository default branch is missing")
            repository_id = repository_payload.get("id")
            repository_identity = (
                f"github-id:{repository_id}"
                if isinstance(repository_id, int)
                and not isinstance(repository_id, bool)
                and repository_id > 0
                else repo.lower()
            )
            commit = self.github.get_commit(repo, default_branch)
            current_head = str(commit.get("sha", "")).lower()
            if current_head != probe.default_head:
                raise PolicyError("repository probe default head changed")
            project_text = self.github.get_repository_file(
                repo,
                ".codex-worker/project.toml",
                ref=current_head,
            )
            project = parse_project_config(project_text)
            if project.default_base_branch != default_branch:
                raise PolicyError("project config default branch does not match repository")
            self._validate_project_worker_app(project)
            config_hash = hashlib.sha256(project_text.encode("utf-8")).hexdigest()
            if config_hash != probe.project_config_hash:
                raise PolicyError("repository probe project config changed")
            attested_at, retry_failed, state_key, retry_state = (
                self._repository_attestation_state(
                    repository_identity=repository_identity,
                    issue_number=number,
                    probe_id=probe.probe_id,
                    default_head=current_head,
                    project_config_hash=config_hash,
                    issue_updated_at=str(issue.get("updated_at", "")),
                )
            )
            attestation_body = render_repository_attestation(
                probe_id=probe.probe_id,
                worker_id=self.config.worker_id,
                default_head=current_head,
                project_config_hash=config_hash,
                attested_at=attested_at,
            )
            retry_comment = getattr(self.github, "retry_failed_comment", None)
            if retry_failed and callable(retry_comment):
                retry_comment(
                    repo,
                    number,
                    attestation_body,
                    state_key=state_key,
                    state_value=retry_state,
                )
            else:
                self.github.add_comment(repo, number, attestation_body)
                if retry_failed:
                    self.store.set_worker_state(state_key, retry_state)
            labels = [
                label
                for label in self._labels(issue)
                if not label.startswith(STATUS_PREFIX)
            ]
            labels.append("codex:completed")
            self.github.update_issue(repo, number, labels=labels, state="closed")
        except Exception as exc:
            labels = [
                label
                for label in self._labels(issue)
                if not label.startswith(STATUS_PREFIX)
            ]
            labels.append("codex:needs-attention")
            self.github.update_issue(repo, number, labels=labels, state="open")
            self.github.add_comment(
                repo,
                number,
                self._status_body(
                    task_hash="repository-probe",
                    state="needs-attention",
                    branch="",
                    progress_at=iso_now(),
                    detail=f"{type(exc).__name__}: {exc}"[:4000],
                ),
            )

    def _require_completed_result(self, result: RunnerResult, spec: Any) -> dict[str, Any]:
        try:
            payload = json.loads(result.last_message)
        except json.JSONDecodeError as exc:
            raise PolicyError("Codex returned an invalid structured result") from exc
        if not isinstance(payload, dict):
            raise PolicyError("Codex returned an invalid structured result")
        if payload.get("status") != "completed":
            raise PolicyError("Codex reported blocked instead of completed")
        required_keys = {
            "status",
            "summary",
            "changed_files",
            "risks",
            "needs_human",
            "acceptance_results",
        }
        if set(payload) != required_keys:
            raise PolicyError("Codex result fields do not match the frozen result schema")
        if not isinstance(payload["summary"], str) or not payload["summary"].strip():
            raise PolicyError("Codex result summary must not be empty")
        if not isinstance(payload["changed_files"], list) or not all(
            isinstance(item, str) for item in payload["changed_files"]
        ):
            raise PolicyError("Codex changed_files must be a string list")

        acceptance_results = payload.get("acceptance_results")
        if not isinstance(acceptance_results, list) or len(acceptance_results) != len(
            spec.acceptance
        ):
            raise PolicyError("Codex acceptance results do not match the frozen task")
        for criterion, result_item in zip(spec.acceptance, acceptance_results, strict=True):
            if not isinstance(result_item, dict) or result_item.get("criterion") != criterion:
                raise PolicyError("Codex acceptance results do not match the frozen task")
            status = result_item.get("status")
            evidence = result_item.get("evidence")
            if status not in {"met", "not_met", "needs_review"}:
                raise PolicyError("Codex returned an invalid acceptance status")
            if not isinstance(evidence, str) or not evidence.strip():
                raise PolicyError("Codex acceptance evidence must not be empty")
            if status == "not_met":
                raise PolicyError(f"Codex did not meet acceptance criterion: {criterion}")
        for key in ("risks", "needs_human"):
            value = payload.get(key)
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                raise PolicyError(f"Codex result field {key} must be a string list")
        return payload

    def _delivery_pr_body(
        self,
        *,
        issue_number: int,
        spec: Any,
        task_hash: str,
        commit_sha: str,
        runner_result: RunnerResult,
        structured_result: dict[str, Any],
        verification_result: Any,
        task_commit_sha: str | None = None,
        integrated_base_sha: str | None = None,
    ) -> str:
        acceptance_results = tuple(structured_result["acceptance_results"])
        risks = tuple(structured_result["risks"])
        needs_human = tuple(structured_result["needs_human"])
        metadata = DeliveryMetadata(
            issue_number=issue_number,
            task_hash=task_hash,
            context_commit=spec.context_commit,
            delivery_commit=commit_sha,
            verification_profile=spec.verification_profile,
            verification_passed=bool(verification_result.passed),
            model=runner_result.model,
            cli_version=runner_result.cli_version,
            acceptance_results=acceptance_results,
            risks=risks,
            needs_human=needs_human,
            task_commit=task_commit_sha,
            integrated_base=integrated_base_sha,
        )
        acceptance_text = "\n".join(
            f"- [{'x' if item['status'] == 'met' else ' '}] {item['criterion']} "
            f"— {item['status']}: {item['evidence']}"
            for item in acceptance_results
        )
        risk_text = "\n".join(f"- {item}" for item in risks) or "- None reported"
        human_text = "\n".join(f"- {item}" for item in needs_human) or "- None"
        merge_note = (
            "The Worker will auto-merge only after the trusted single-owner gates pass."
            if self.config.merge_mode == "automatic"
            else "This repository remains in manual merge mode."
        )
        return f"""{render_delivery_block(metadata)}
Relates to #{issue_number}

## Acceptance
{acceptance_text}

## Verification
```text
{self._verification_detail(verification_result)}
```

## Risks
{risk_text}

## Human dependencies
{human_text}

This PR was created as a draft. {merge_note}
"""

    def _validate_delivery_diff(
        self,
        worktree: Path,
        baseline_head: str,
        spec: Any,
        project_config: Any,
    ) -> None:
        self.git.assert_head_unchanged(worktree, baseline_head)
        diff = self.git.diff_summary(worktree, spec.context_commit)
        if not diff.changed_paths:
            raise PolicyError("Codex produced no repository changes")
        validate_changed_paths(spec, project_config, diff.changed_paths, diff.diff_lines)
        scan_for_secrets(worktree, diff.changed_paths)

    def _validate_committed_delivery(
        self,
        worktree: Path,
        baseline_head: str,
        spec: Any,
        project_config: Any,
    ) -> None:
        if not self.git.is_clean(worktree):
            raise PolicyError("delivery worktree is not clean")
        diff = self.git.diff_summary(worktree, baseline_head)
        if not diff.changed_paths:
            raise PolicyError("delivery commit has no repository changes")
        validate_changed_paths(spec, project_config, diff.changed_paths, diff.diff_lines)
        scan_for_secrets(worktree, diff.changed_paths)

    def _verification_detail(self, result: Any) -> str:
        return "\n\n".join(
            f"$ {item.command}\nexit={item.exit_code}\n{item.output[-3000:]}"
            for item in result.commands
        )

    def _serialize_verification(self, result: VerificationResult) -> dict[str, Any]:
        return {
            "passed": result.passed,
            "termination_reason": result.termination_reason,
            "commands": [
                {
                    "command": item.command,
                    "exit_code": item.exit_code,
                    "output": item.output[-3000:],
                }
                for item in result.commands
            ],
        }

    def _project_config_hash(self, worktree: Path) -> str:
        text = (worktree / ".codex-worker" / "project.toml").read_text(
            encoding="utf-8"
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _restore_verification(self, payload: dict[str, Any]) -> VerificationResult:
        commands = tuple(
            CommandResult(
                command=str(item["command"]),
                exit_code=int(item["exit_code"]),
                output=str(item["output"]),
            )
            for item in payload["commands"]
        )
        return VerificationResult(
            passed=bool(payload["passed"]),
            commands=commands,
            termination_reason=payload.get("termination_reason"),
        )

    def _set_delivery_failure(
        self,
        repo: str,
        issue_number: int,
        task_hash: str,
        phase: str,
        exc: Exception,
    ) -> None:
        self.store.set_delivery_checkpoint_state(
            repo,
            issue_number,
            task_hash,
            phase=phase,
            retryable=getattr(exc, "retryable", False) is True,
            last_error=f"{type(exc).__name__}: {exc}",
        )

    @staticmethod
    def _require_delivery_deadline(
        deadline_monotonic: float | None,
        phase: str,
    ) -> None:
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise RunnerTimeout(
                f"delivery retry hard timeout exceeded before {phase}"
            )

    def _checkpoint_runner_result(
        self,
        checkpoint: dict[str, Any],
    ) -> RunnerResult:
        return RunnerResult(
            exit_code=0,
            session_id=checkpoint.get("session_id"),
            events=(),
            last_message=json.dumps(
                checkpoint["structured_result"],
                ensure_ascii=False,
                sort_keys=True,
            ),
            stderr="",
            model=checkpoint.get("model"),
            cli_version=checkpoint.get("cli_version"),
        )

    def _deliver_checkpoint(
        self,
        repository: RepositoryConfig,
        issue: dict[str, Any],
        spec: Any,
        checkpoint: dict[str, Any],
        verification_result: VerificationResult,
        *,
        status_comment_id: int,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        repo = repository.name
        number = int(issue["number"])
        task_hash = str(checkpoint["task_hash"])
        branch = str(checkpoint["branch"])
        worktree = Path(str(checkpoint["worktree"]))
        runner_result = self._checkpoint_runner_result(checkpoint)
        pr_body = self._delivery_pr_body(
            issue_number=number,
            spec=spec,
            task_hash=task_hash,
            commit_sha=str(checkpoint["commit_sha"]),
            runner_result=runner_result,
            structured_result=checkpoint["structured_result"],
            verification_result=verification_result,
            task_commit_sha=str(checkpoint["task_commit_sha"]),
            integrated_base_sha=str(checkpoint["integrated_base_sha"]),
        )
        try:
            self._require_delivery_deadline(deadline_monotonic, "push")
            self.git.push(
                worktree,
                branch=branch,
                clone_url=repository.clone_url,
                token=self.token_provider(),
                deadline_monotonic=deadline_monotonic,
            )
        except Exception as exc:
            self._set_delivery_failure(repo, number, task_hash, "push", exc)
            raise
        try:
            self._require_delivery_deadline(deadline_monotonic, "pull request")
            pr = self.github.create_draft_pr(
                repo,
                branch,
                spec.base_branch,
                f"[Codex #{number}] {issue.get('title', 'Task')}",
                pr_body,
            )
        except Exception as exc:
            self._set_delivery_failure(repo, number, task_hash, "pull-request", exc)
            raise
        try:
            self._require_delivery_deadline(deadline_monotonic, "finalization")
            delivery_state = (
                "merging" if self.config.merge_mode == "automatic" else "awaiting-review"
            )
            self._set_state(repo, issue, delivery_state)
            self._require_delivery_deadline(deadline_monotonic, "status comment")
            self.github.update_comment(
                repo,
                status_comment_id,
                self._status_body(
                    task_hash=task_hash,
                    state=delivery_state,
                    branch=branch,
                    progress_at=iso_now(),
                    detail=str(pr.get("html_url", "")),
                ),
            )
            self._require_delivery_deadline(deadline_monotonic, "local finalization")
            self.store.upsert_task(
                repo=repo,
                issue_number=number,
                task_hash=task_hash,
                state=delivery_state,
                branch=branch,
                worktree=str(worktree),
                session_id=checkpoint.get("session_id"),
                pr_number=int(pr["number"]),
            )
        except Exception as exc:
            self._set_delivery_failure(repo, number, task_hash, "finalize", exc)
            raise
        self.store.set_delivery_checkpoint_state(
            repo,
            number,
            task_hash,
            phase="complete",
            retryable=False,
            last_error=None,
        )
        return pr

    def auto_merge_delivery(
        self,
        repository: RepositoryConfig,
        issue: dict[str, Any],
        task: dict[str, Any],
    ) -> str:
        """Revalidate and auto-merge a retained delivery without invoking Codex."""
        repo = repository.name
        number = int(issue["number"])
        task_hash = str(task.get("task_hash", ""))
        attempt_key = f"auto-merge-attempts:{repo}#{number}:{task_hash}"
        try:
            spec = parse_task_body(str(issue.get("body", "")))
            if spec.task_hash != task_hash:
                raise PolicyError("task body changed before automatic merge")
            if self.config.merge_mode != "automatic":
                raise PolicyError("local merge mode is not automatic")
            pr_number = task.get("pr_number")
            if not isinstance(pr_number, int) or isinstance(pr_number, bool):
                raise PolicyError("automatic merge task has no PR number")
            checkpoint = self.store.get_delivery_checkpoint(repo, number, task_hash)
            if checkpoint is None:
                raise PolicyError("automatic merge delivery checkpoint is missing")
            branch = str(checkpoint["branch"])
            worktree = Path(str(checkpoint["worktree"]))
            if not worktree.is_dir():
                raise PolicyError("automatic merge worktree is missing")
            if self.git.current_branch(worktree) != branch:
                raise PolicyError("automatic merge branch changed after checkpoint")
            if self.git.current_head(worktree) != str(checkpoint["commit_sha"]):
                raise PolicyError("automatic merge HEAD changed after checkpoint")
            if not self.git.is_clean(worktree):
                raise PolicyError("automatic merge worktree is not clean")
            self.validate_repository_authority(repository)

            # A merge API call may have succeeded before its response was
            # persisted. Reconcile that exact remote result before refreshing
            # main: the squash merge now changes the same paths as the task and
            # would correctly look like an integration conflict otherwise.
            pull = self.github.get_pull_request(repo, pr_number)
            if pull.get("merged_at"):
                result = automatic_merge_task(
                    self.github,
                    self.store,
                    IssueReference(repo, number),
                    pr_number=pr_number,
                    expected_head=str(checkpoint["commit_sha"]),
                    merge_mode=self.config.merge_mode,
                )
                self.store.set_worker_state(attempt_key, 0)
                return "completed" if result.merged else "merging"

            mirror = self.git.mirror_path(repo)
            if not mirror.is_dir():
                raise PolicyError("automatic merge repository mirror is missing")
            self.git.refresh_branch(
                mirror,
                clone_url=repository.clone_url,
                branch=spec.base_branch,
                token=self.token_provider(),
            )
            task_paths = self.git.changed_paths_between(
                worktree,
                spec.context_commit,
                str(checkpoint["task_commit_sha"]),
            )
            previous_head = str(checkpoint["commit_sha"])
            integration = self.git.integrate_default(
                worktree,
                mirror,
                spec.base_branch,
                str(checkpoint["integrated_base_sha"]),
                task_paths,
                refresh_count=int(checkpoint["integration_refreshes"]),
                author_name="Codex Mac Worker",
                author_email="codex-worker@users.noreply.github.com",
            )
            if integration.delivery_head != previous_head:
                project_config = load_project_config(
                    worktree / ".codex-worker" / "project.toml"
                )
                self._validate_project_worker_app(project_config)
                validate_task_policy(spec, project_config)
                self._validate_committed_delivery(
                    worktree,
                    integration.integrated_base,
                    spec,
                    project_config,
                )
                verification_result = run_verification(
                    worktree,
                    project_config,
                    spec.verification_profile,
                    timeout_seconds=1800,
                    codex_path=(
                        self.config.codex_path if self.config.codex_home else None
                    ),
                    codex_home=self.config.codex_home,
                    control_callback=self._command_monitor(repo, number),
                )
                if verification_result.termination_reason or not verification_result.passed:
                    raise PolicyError(
                        "automatic merge integration verification failed:\n"
                        + self._verification_detail(verification_result)
                    )
                self.store.update_delivery_integration(
                    repo,
                    number,
                    task_hash,
                    expected_task_commit=str(checkpoint["task_commit_sha"]),
                    previous_head=previous_head,
                    delivery_head=integration.delivery_head,
                    integrated_base=integration.integrated_base,
                    integration_refreshes=integration.refresh_count,
                    verification_result=self._serialize_verification(
                        verification_result
                    ),
                    verification_commands=project_config.verification[
                        spec.verification_profile
                    ],
                    project_config_hash=self._project_config_hash(worktree),
                )
                checkpoint = self.store.get_delivery_checkpoint(
                    repo, number, task_hash
                )
                assert checkpoint is not None

            # Always reconcile the checkpointed head with the remote branch. A
            # previous process may have persisted an integration refresh and
            # then failed before push; the next pass must finish that same
            # delivery instead of invoking Codex or creating another commit.
            self.git.push(
                worktree,
                branch=branch,
                clone_url=repository.clone_url,
                token=self.token_provider(),
            )
            runner_result = self._checkpoint_runner_result(checkpoint)
            pr_body = self._delivery_pr_body(
                issue_number=number,
                spec=spec,
                task_hash=task_hash,
                commit_sha=str(checkpoint["commit_sha"]),
                runner_result=runner_result,
                structured_result=checkpoint["structured_result"],
                verification_result=self._restore_verification(
                    checkpoint["verification_result"]
                ),
                task_commit_sha=str(checkpoint["task_commit_sha"]),
                integrated_base_sha=str(checkpoint["integrated_base_sha"]),
            )
            self.github.update_pull_request(repo, pr_number, body=pr_body)

            result = automatic_merge_task(
                self.github,
                self.store,
                IssueReference(repo, number),
                pr_number=pr_number,
                expected_head=str(checkpoint["commit_sha"]),
                merge_mode=self.config.merge_mode,
            )
            self.store.set_worker_state(attempt_key, 0)
            return "completed" if result.merged else "merging"
        except Exception as exc:
            attempts = int(self.store.get_worker_state(attempt_key, 0)) + 1
            self.store.set_worker_state(attempt_key, attempts)
            self.store.set_worker_state(
                attempt_key + ":last-error",
                {"error": f"{type(exc).__name__}: {exc}"[:4000], "attempts": attempts},
            )
            retryable = getattr(exc, "retryable", False) is True
            if retryable and attempts < 2:
                return "merging"
            return "needs-attention"

    def _command_monitor(self, repo: str, issue_number: int) -> Callable[[], str | None]:
        last_checked = 0.0
        seen = {item["command_id"] for item in self.store.pending_commands(repo, issue_number)}

        def check() -> str | None:
            nonlocal last_checked
            if self._stopping:
                return "pause"
            now = time.monotonic()
            if now - last_checked < 10:
                return None
            last_checked = now
            for comment in self.github.list_comments(repo, issue_number):
                body = comment.get("body", "")
                if "<!-- codex-command:v1 -->" not in body:
                    continue
                try:
                    command = parse_command_comment(body)
                except ProtocolError:
                    continue
                if command.command_id in seen or command.issue_number != issue_number:
                    continue
                author = str(comment.get("user", {}).get("login", ""))
                permission = self.github.collaborator_permission(repo, author)
                if author not in self.config.authorized_users or permission not in AUTHORIZED_PERMISSIONS:
                    continue
                seen.add(command.command_id)
                self.store.record_command(command.command_id, repo, issue_number, command.action, author)
                if command.action in {"pause", "cancel"}:
                    self.store.mark_command_executed(command.command_id, command.action)
                    return command.action
            return None

        return check

    def process_issue(
        self,
        repository: RepositoryConfig,
        issue: dict[str, Any],
        resume_session_id: str | None = None,
    ) -> None:
        repo = repository.name
        number = int(issue["number"])
        task_hash = "invalid"
        branch = ""
        try:
            self._validate_issue_author(repo, issue)
            spec = parse_task_body(str(issue.get("body", "")))
            self.validate_repository_authority(repository)
            conflicts = active_task_conflicts(
                self.github,
                repo,
                spec.allowed_paths,
                exclude_issue_number=number,
                ignore_queued=True,
            )
            if conflicts:
                raise PolicyError(
                    "allowed_paths conflict with active Worker task: "
                    + ", ".join(conflicts)
                )
            task_hash = spec.task_hash
            branch = f"codex/{number}-{_slug(str(issue.get('title', 'task')))}"
            self.store.upsert_task(
                repo=repo,
                issue_number=number,
                task_hash=task_hash,
                state="claimed",
                branch=branch,
            )
            self._set_state(repo, issue, "claimed")
            progress_at = iso_now()
            status = self.github.add_comment(
                repo,
                number,
                self._status_body(
                    task_hash=task_hash,
                    state="claimed",
                    branch=branch,
                    progress_at=progress_at,
                ),
            )
            status_comment_id = int(status["id"])

            token = self.token_provider()
            mirror = self.git.ensure_mirror(repo, repository.clone_url, token)
            # ensure_mirror refreshes remote-tracking refs without touching
            # branches that may be checked out by retained task worktrees.
            # Refresh only the requested base branch into refs/heads before
            # validating the frozen context commit against it.
            self.git.refresh_branch(
                mirror,
                clone_url=repository.clone_url,
                branch=spec.base_branch,
                token=token,
            )
            prepared = self.git.prepare_worktree(
                repo=repo,
                mirror=mirror,
                context_commit=spec.context_commit,
                base_branch=spec.base_branch,
                issue_number=number,
                slug=_slug(str(issue.get("title", "task"))),
            )
            branch = prepared.branch
            project_config = load_project_config(prepared.path / ".codex-worker/project.toml")
            self._validate_project_worker_app(project_config)
            self._validate_runtime_policy(prepared.path)
            validate_task_policy(spec, project_config)
            self._validate_context_files(prepared.path, spec.context_files)
            task_record = self.store.get_task(repo, number)
            assert task_record is not None and task_record.get("claimed_at")
            claimed_at = datetime.fromisoformat(str(task_record["claimed_at"]))
            hard_deadline = claimed_at + timedelta(
                minutes=project_config.task_hard_timeout_minutes
            )
            remaining = (hard_deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise RunnerTimeout("task hard timeout exceeded before preparation")
            monitor = self._command_monitor(repo, number)
            preparation_result = run_commands(
                prepared.path,
                project_config.preparation,
                timeout_seconds=max(1, min(remaining, 1800)),
                codex_path=self.config.codex_path if self.config.codex_home else None,
                codex_home=self.config.codex_home,
                permission_profile="codex-worker-preparation",
                control_callback=monitor,
            )
            if preparation_result.termination_reason:
                state = (
                    "cancelled"
                    if preparation_result.termination_reason == "cancel"
                    else "paused"
                )
                self.store.upsert_task(
                    repo=repo,
                    issue_number=number,
                    task_hash=task_hash,
                    state=state,
                    branch=branch,
                    worktree=str(prepared.path),
                )
                self._set_state(repo, issue, "cancelled" if state == "cancelled" else "claimed")
                return
            if not preparation_result.passed:
                raise RuntimeError("preparation failed:\n" + self._verification_detail(preparation_result))

            self._set_state(repo, issue, "running")
            self.store.upsert_task(
                repo=repo,
                issue_number=number,
                task_hash=task_hash,
                state="running",
                branch=branch,
                worktree=str(prepared.path),
            )
            progress_at = iso_now()
            status_state = "running"
            hard_deadline_at = hard_deadline.isoformat()
            if datetime.now(UTC) >= hard_deadline:
                raise RunnerTimeout("task hard timeout exceeded before execution")
            self.config.output_root.mkdir(parents=True, exist_ok=True)
            schema_path = self.config.output_root / "result.schema.json"
            schema_path.write_text(
                json.dumps(result_schema(spec), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            def heartbeat() -> None:
                self.github.update_comment(
                    repo,
                    status_comment_id,
                    self._status_body(
                        task_hash=task_hash,
                        state=status_state,
                        branch=branch,
                        progress_at=progress_at,
                        hard_deadline_at=hard_deadline_at,
                    ),
                )

            heartbeat()

            prompt = build_execution_prompt(spec, issue_number=number)
            verification_result = None
            runner_result = None
            structured_result = None
            for attempt in range(1, project_config.max_automatic_attempts + 1):
                remaining = (hard_deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise RunnerTimeout("task hard timeout exceeded")
                run_id = self.store.start_run(repo, number)
                runner_result = self.runner.run(
                    prepared.path,
                    prompt,
                    schema_path,
                    timeout_seconds=min(
                        project_config.codex_attempt_timeout_minutes * 60,
                        remaining,
                    ),
                    heartbeat_callback=heartbeat,
                    heartbeat_interval_seconds=self.config.heartbeat_seconds,
                    control_callback=monitor,
                    resume_session_id=resume_session_id if attempt == 1 else None,
                )
                self.store.finish_run(
                    run_id,
                    exit_code=runner_result.exit_code,
                    result={
                        "session_id": runner_result.session_id,
                        "termination_reason": runner_result.termination_reason,
                        "event_count": len(runner_result.events),
                        "last_message": runner_result.last_message[:8000],
                        "model": runner_result.model,
                        "cli_version": runner_result.cli_version,
                    },
                )
                if runner_result.termination_reason == "cancel":
                    self.store.upsert_task(
                        repo=repo,
                        issue_number=number,
                        task_hash=task_hash,
                        state="cancelled",
                        branch=branch,
                        worktree=str(prepared.path),
                        session_id=runner_result.session_id,
                    )
                    self._set_state(repo, issue, "cancelled")
                    return
                if runner_result.termination_reason == "pause":
                    self.store.upsert_task(
                        repo=repo,
                        issue_number=number,
                        task_hash=task_hash,
                        state="paused",
                        branch=branch,
                        worktree=str(prepared.path),
                        session_id=runner_result.session_id,
                    )
                    self._set_state(repo, issue, "claimed")
                    return
                if runner_result.exit_code != 0:
                    if attempt == project_config.max_automatic_attempts:
                        raise RuntimeError(f"Codex exited {runner_result.exit_code}: {runner_result.stderr}")
                    status_state = "retrying"
                    progress_at = iso_now()
                    self._set_state(repo, issue, "retrying")
                    self.store.upsert_task(
                        repo=repo,
                        issue_number=number,
                        task_hash=task_hash,
                        state="retrying",
                        branch=branch,
                        worktree=str(prepared.path),
                        session_id=runner_result.session_id,
                    )
                    heartbeat()
                    continue

                structured_result = self._require_completed_result(runner_result, spec)
                self._validate_delivery_diff(
                    prepared.path, prepared.baseline_head, spec, project_config
                )
                self._set_state(repo, issue, "verifying")
                status_state = "verifying"
                progress_at = iso_now()
                self.store.upsert_task(
                    repo=repo,
                    issue_number=number,
                    task_hash=task_hash,
                    state="verifying",
                    branch=branch,
                    worktree=str(prepared.path),
                    session_id=runner_result.session_id,
                )
                heartbeat()
                remaining = (hard_deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise RunnerTimeout("task hard timeout exceeded before verification")
                verification_result = run_verification(
                    prepared.path,
                    project_config,
                    spec.verification_profile,
                    timeout_seconds=max(1, min(remaining, 1800)),
                    codex_path=self.config.codex_path if self.config.codex_home else None,
                    codex_home=self.config.codex_home,
                    control_callback=monitor,
                )
                if verification_result.termination_reason:
                    state = (
                        "cancelled"
                        if verification_result.termination_reason == "cancel"
                        else "paused"
                    )
                    self.store.upsert_task(
                        repo=repo,
                        issue_number=number,
                        task_hash=task_hash,
                        state=state,
                        branch=branch,
                        worktree=str(prepared.path),
                        session_id=runner_result.session_id,
                    )
                    self._set_state(
                        repo, issue, "cancelled" if state == "cancelled" else "claimed"
                    )
                    return
                if verification_result.passed:
                    break
                if attempt == project_config.max_automatic_attempts:
                    raise RuntimeError("verification failed:\n" + self._verification_detail(verification_result))
                status_state = "retrying"
                progress_at = iso_now()
                self._set_state(repo, issue, "retrying")
                self.store.upsert_task(
                    repo=repo,
                    issue_number=number,
                    task_hash=task_hash,
                    state="retrying",
                    branch=branch,
                    worktree=str(prepared.path),
                    session_id=runner_result.session_id,
                )
                heartbeat()
                prompt = build_revision_prompt(
                    spec,
                    "Fix only the verification failures shown below.",
                    self._verification_detail(verification_result),
                )

            assert (
                runner_result is not None
                and verification_result is not None
                and structured_result is not None
            )
            latest_issue = self.github.get_issue(repo, number)
            if parse_task_body(str(latest_issue.get("body", ""))).task_hash != task_hash:
                raise PolicyError("task body changed after claim")

            self._validate_delivery_diff(
                prepared.path, prepared.baseline_head, spec, project_config
            )
            self.validate_repository_authority(repository)

            task_commit_sha = self.git.commit(
                prepared.path,
                f"feat: complete codex task #{number}",
                author_name="Codex Mac Worker",
                author_email="codex-worker@users.noreply.github.com",
            )
            task_diff = self.git.diff_summary(prepared.path, spec.context_commit)
            self.git.refresh_branch(
                mirror,
                clone_url=repository.clone_url,
                branch=spec.base_branch,
                token=self.token_provider(),
            )
            integration = self.git.integrate_default(
                prepared.path,
                mirror,
                spec.base_branch,
                spec.context_commit,
                task_diff.changed_paths,
                refresh_count=0,
                author_name="Codex Mac Worker",
                author_email="codex-worker@users.noreply.github.com",
            )
            project_config = load_project_config(
                prepared.path / ".codex-worker" / "project.toml"
            )
            self._validate_project_worker_app(project_config)
            validate_task_policy(spec, project_config)
            self._validate_committed_delivery(
                prepared.path,
                integration.integrated_base,
                spec,
                project_config,
            )
            if integration.refresh_count:
                remaining = (hard_deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise RunnerTimeout(
                        "task hard timeout exceeded before integration verification"
                    )
                verification_result = run_verification(
                    prepared.path,
                    project_config,
                    spec.verification_profile,
                    timeout_seconds=max(1, min(remaining, 1800)),
                    codex_path=(
                        self.config.codex_path if self.config.codex_home else None
                    ),
                    codex_home=self.config.codex_home,
                    control_callback=monitor,
                )
                if not verification_result.passed:
                    raise PolicyError(
                        "integration verification failed:\n"
                        + self._verification_detail(verification_result)
                    )
            self.store.save_delivery_checkpoint(
                repo=repo,
                issue_number=number,
                task_hash=task_hash,
                branch=branch,
                worktree=str(prepared.path),
                context_commit=spec.context_commit,
                commit_sha=integration.delivery_head,
                task_commit_sha=task_commit_sha,
                integrated_base_sha=integration.integrated_base,
                integration_refreshes=integration.refresh_count,
                project_config_hash=self._project_config_hash(prepared.path),
                verification_profile=spec.verification_profile,
                verification_commands=project_config.verification[
                    spec.verification_profile
                ],
                verification_result=self._serialize_verification(verification_result),
                structured_result=structured_result,
                model=runner_result.model,
                cli_version=runner_result.cli_version,
                session_id=runner_result.session_id,
            )
            checkpoint = self.store.get_delivery_checkpoint(repo, number, task_hash)
            assert checkpoint is not None
            self._deliver_checkpoint(
                repository,
                issue,
                spec,
                checkpoint,
                verification_result,
                status_comment_id=status_comment_id,
            )
        except Exception as exc:
            self._mark_attention(repo, issue, task_hash, branch, f"{type(exc).__name__}: {exc}")

    def _legacy_delivery_checkpoint_candidate(
        self,
        repo: str,
        issue_number: int,
        task: dict[str, Any],
        spec: Any,
    ) -> dict[str, Any]:
        branch = str(task.get("branch") or "")
        worktree_value = str(task.get("worktree") or "")
        session_id = task.get("session_id")
        if not branch or not worktree_value or not session_id:
            raise PolicyError("legacy delivery task evidence is incomplete")
        worktree = Path(worktree_value)
        if not worktree.is_dir():
            raise PolicyError("delivery worktree is missing")

        runs = [
            run
            for run in self.store.list_runs(repo, issue_number)
            if run["finished_at"]
            and run["exit_code"] == 0
            and isinstance(run.get("result"), dict)
            and run["result"].get("termination_reason") is None
            and run["result"].get("session_id") == session_id
        ]
        if len(runs) != 1:
            raise PolicyError("legacy delivery requires one matching successful run")
        run_result = runs[0]["result"]
        last_message = run_result.get("last_message")
        if not isinstance(last_message, str) or not last_message:
            raise PolicyError("legacy delivery final message is missing")
        runner_result = RunnerResult(
            exit_code=0,
            session_id=str(session_id),
            events=(),
            last_message=last_message,
            stderr="",
            model=run_result.get("model"),
            cli_version=run_result.get("cli_version"),
        )
        structured_result = self._require_completed_result(runner_result, spec)
        project_config = load_project_config(
            worktree / ".codex-worker" / "project.toml"
        )
        verification_commands = project_config.verification.get(
            spec.verification_profile
        )
        if verification_commands is None:
            raise PolicyError("legacy delivery verification profile is missing")
        return {
            "repo": repo,
            "issue_number": issue_number,
            "task_hash": spec.task_hash,
            "branch": branch,
            "worktree": worktree_value,
            "context_commit": spec.context_commit,
            "commit_sha": self.git.current_head(worktree),
            "task_commit_sha": self.git.current_head(worktree),
            "integrated_base_sha": spec.context_commit,
            "integration_refreshes": 0,
            "project_config_hash": self._project_config_hash(worktree),
            "verification_profile": spec.verification_profile,
            "verification_commands": list(verification_commands),
            "verification_result": {"passed": False, "commands": []},
            "structured_result": structured_result,
            "model": runner_result.model,
            "cli_version": runner_result.cli_version,
            "session_id": runner_result.session_id,
            "phase": "legacy-reconstruction",
            "retryable": True,
        }

    def retry_delivery(
        self,
        repository: RepositoryConfig,
        issue: dict[str, Any],
    ) -> str:
        """Retry only a retained verified delivery commit; never invoke Codex."""
        deadline = time.monotonic() + DELIVERY_RETRY_TIMEOUT_SECONDS
        deadline_at = datetime.now(UTC) + timedelta(
            seconds=DELIVERY_RETRY_TIMEOUT_SECONDS
        )
        scope_factory = getattr(self.github, "request_deadline", None)
        scope = (
            scope_factory(deadline)
            if scope_factory is not None
            else nullcontext()
        )
        with scope:
            return self._retry_delivery_bounded(
                repository,
                issue,
                deadline=deadline,
                deadline_at=deadline_at,
            )

    def _retry_delivery_bounded(
        self,
        repository: RepositoryConfig,
        issue: dict[str, Any],
        *,
        deadline: float,
        deadline_at: datetime,
    ) -> str:
        repo = repository.name
        number = int(issue["number"])
        task = self.store.get_task(repo, number)
        task_hash = str(task.get("task_hash", "invalid")) if task else "invalid"
        branch = str(task.get("branch") or "") if task else ""
        checkpoint = (
            self.store.get_delivery_checkpoint(repo, number, task_hash)
            if task is not None
            else None
        )
        attempted_legacy = checkpoint is None
        legacy_key = f"legacy-delivery-recovery:{repo}#{number}:{task_hash}"
        try:
            if task is None:
                raise PolicyError("delivery retry task record is missing")

            self._validate_issue_author(repo, issue)
            self._require_delivery_deadline(deadline, "task authorization")
            spec = parse_task_body(str(issue.get("body", "")))
            if spec.task_hash != task_hash:
                raise PolicyError("task body changed after delivery checkpoint")
            self.validate_repository_authority(repository)
            self._require_delivery_deadline(deadline, "repository authorization")
            if checkpoint is None:
                if self.store.get_worker_state(legacy_key) == "rejected":
                    raise PolicyError("legacy delivery reconstruction was rejected")
                checkpoint = self._legacy_delivery_checkpoint_candidate(
                    repo,
                    number,
                    task,
                    spec,
                )
            elif checkpoint.get("retryable") is not True:
                raise PolicyError("delivery checkpoint is not retryable")
            if branch != str(checkpoint["branch"]):
                raise PolicyError("delivery branch changed after checkpoint")
            if str(task.get("worktree") or "") != str(checkpoint["worktree"]):
                raise PolicyError("delivery worktree changed after checkpoint")
            worktree = Path(str(checkpoint["worktree"]))
            if not worktree.is_dir():
                raise PolicyError("delivery worktree is missing")
            if self.git.current_branch(worktree) != branch:
                raise PolicyError("delivery branch changed after checkpoint")
            if not self.git.is_clean(worktree):
                raise PolicyError("delivery worktree is not clean")
            if self.git.current_head(worktree) != str(checkpoint["commit_sha"]):
                raise PolicyError("delivery HEAD changed after checkpoint")
            delivery_parents = self.git.commit_parents(
                worktree, str(checkpoint["commit_sha"])
            )
            task_commit_sha = str(checkpoint["task_commit_sha"])
            integrated_base_sha = str(checkpoint["integrated_base_sha"])
            refreshes = int(checkpoint["integration_refreshes"])
            if self.git.commit_parents(worktree, task_commit_sha) != (
                spec.context_commit,
            ):
                raise PolicyError("task commit must have the context commit as sole parent")
            if refreshes == 0:
                if (
                    str(checkpoint["commit_sha"]) != task_commit_sha
                    or integrated_base_sha != spec.context_commit
                    or delivery_parents != (spec.context_commit,)
                ):
                    raise PolicyError("non-integrated delivery checkpoint is inconsistent")
            elif (
                len(delivery_parents) != 2
                or delivery_parents[1] != integrated_base_sha
                or not self.git.is_ancestor(
                    worktree, task_commit_sha, delivery_parents[0]
                )
            ):
                raise PolicyError("integration delivery parents differ from checkpoint")
            project_config = load_project_config(
                worktree / ".codex-worker" / "project.toml"
            )
            self._validate_project_worker_app(project_config)
            self._validate_runtime_policy(worktree)
            validate_task_policy(spec, project_config)
            self._validate_context_files(worktree, spec.context_files)
            if self._project_config_hash(worktree) != str(
                checkpoint["project_config_hash"]
            ):
                raise PolicyError("delivery project config changed after checkpoint")
            if str(checkpoint["verification_profile"]) != spec.verification_profile:
                raise PolicyError("delivery verification profile changed after checkpoint")
            verification_commands = project_config.verification.get(
                spec.verification_profile
            )
            if verification_commands is None or tuple(
                checkpoint["verification_commands"]
            ) != verification_commands:
                raise PolicyError("delivery verification commands changed after checkpoint")

            diff = self.git.diff_summary(worktree, integrated_base_sha)
            if not diff.changed_paths:
                raise PolicyError("delivery commit has no repository changes")
            validate_changed_paths(
                spec,
                project_config,
                diff.changed_paths,
                diff.diff_lines,
            )
            scan_for_secrets(worktree, diff.changed_paths)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RunnerTimeout(
                    "delivery retry hard timeout exceeded before verification"
                )
            self.store.upsert_task(
                repo=repo,
                issue_number=number,
                task_hash=task_hash,
                state="retrying",
                branch=branch,
                worktree=str(worktree),
                session_id=task.get("session_id"),
            )
            self._require_delivery_deadline(deadline, "retry status label")
            self._set_state(repo, issue, "retrying")
            self._require_delivery_deadline(deadline, "retry status comment")
            status = self.github.add_comment(
                repo,
                number,
                self._status_body(
                    task_hash=task_hash,
                    state="retrying",
                    branch=branch,
                    progress_at=iso_now(),
                    hard_deadline_at=deadline_at.isoformat(),
                    detail=f"delivery retry phase: {checkpoint['phase']}",
                ),
            )
            status_comment_id = int(status["id"])
            self._require_delivery_deadline(deadline, "verification")
            verification_result = run_verification(
                worktree,
                project_config,
                spec.verification_profile,
                timeout_seconds=min(remaining, 1800),
                codex_path=self.config.codex_path if self.config.codex_home else None,
                codex_home=self.config.codex_home,
                control_callback=self._command_monitor(repo, number),
            )
            if verification_result.termination_reason:
                state = (
                    "cancelled"
                    if verification_result.termination_reason == "cancel"
                    else "paused"
                )
                self.store.set_delivery_checkpoint_state(
                    repo,
                    number,
                    task_hash,
                    phase=(
                        "cancelled"
                        if state == "cancelled"
                        else "paused-verification"
                    ),
                    retryable=state == "paused",
                    last_error=None,
                )
                self.store.upsert_task(
                    repo=repo,
                    issue_number=number,
                    task_hash=task_hash,
                    state=state,
                    branch=branch,
                    worktree=str(worktree),
                    session_id=task.get("session_id"),
                )
                self._set_state(
                    repo,
                    issue,
                    "cancelled" if state == "cancelled" else "claimed",
                )
                return state
            self._require_delivery_deadline(deadline, "delivery checkpoint update")
            if not verification_result.passed:
                raise PolicyError(
                    "delivery retry verification failed:\n"
                    + self._verification_detail(verification_result)
                )
            serialized_verification = self._serialize_verification(verification_result)
            if attempted_legacy:
                self.store.save_delivery_checkpoint(
                    repo=repo,
                    issue_number=number,
                    task_hash=task_hash,
                    branch=branch,
                    worktree=str(worktree),
                    context_commit=spec.context_commit,
                    commit_sha=str(checkpoint["commit_sha"]),
                    task_commit_sha=str(checkpoint["task_commit_sha"]),
                    integrated_base_sha=str(checkpoint["integrated_base_sha"]),
                    integration_refreshes=int(checkpoint["integration_refreshes"]),
                    project_config_hash=str(checkpoint["project_config_hash"]),
                    verification_profile=spec.verification_profile,
                    verification_commands=tuple(
                        checkpoint["verification_commands"]
                    ),
                    verification_result=serialized_verification,
                    structured_result=checkpoint["structured_result"],
                    model=checkpoint.get("model"),
                    cli_version=checkpoint.get("cli_version"),
                    session_id=checkpoint.get("session_id"),
                    phase="legacy-reconstructed",
                    worker_state_key=legacy_key,
                    worker_state_value="reconstructed",
                )
            else:
                self.store.update_delivery_verification(
                    repo,
                    number,
                    task_hash,
                    serialized_verification,
                )

            checkpoint = self.store.get_delivery_checkpoint(repo, number, task_hash)
            assert checkpoint is not None
            try:
                self._deliver_checkpoint(
                    repository,
                    issue,
                    spec,
                    checkpoint,
                    verification_result,
                    status_comment_id=status_comment_id,
                    deadline_monotonic=deadline,
                )
            except Exception as exc:
                self._mark_attention(
                    repo,
                    issue,
                    task_hash,
                    branch,
                    f"{type(exc).__name__}: {exc}",
                    allow_remote=time.monotonic() < deadline,
                )
                return (
                    "needs-attention"
                    if getattr(exc, "retryable", False) is True
                    else "not-retryable"
                )
            return (
                "merging"
                if self.config.merge_mode == "automatic"
                else "awaiting-review"
            )
        except Exception as exc:
            retryable = getattr(exc, "retryable", False) is True
            persisted_checkpoint = self.store.get_delivery_checkpoint(
                repo,
                number,
                task_hash,
            )
            if persisted_checkpoint is not None:
                self.store.set_delivery_checkpoint_state(
                    repo,
                    number,
                    task_hash,
                    phase="preflight" if retryable else "validation",
                    retryable=retryable,
                    last_error=f"{type(exc).__name__}: {exc}",
                )
            elif attempted_legacy and not retryable:
                self.store.set_worker_state(legacy_key, "rejected")
            self._mark_attention(
                repo,
                issue,
                task_hash,
                branch,
                f"{type(exc).__name__}: {exc}",
                allow_remote=time.monotonic() < deadline,
            )
            return "needs-attention" if retryable else "not-retryable"

    def revise_issue(
        self,
        repository: RepositoryConfig,
        issue: dict[str, Any],
        task: dict[str, Any],
        requirements: tuple[str, ...],
    ) -> None:
        """Run one bounded revision session on the existing PR branch."""
        repo = repository.name
        number = int(issue["number"])
        task_hash = str(task["task_hash"])
        branch = str(task.get("branch") or "")
        try:
            spec = parse_task_body(str(issue.get("body", "")))
            self.validate_repository_authority(repository)
            if spec.task_hash != task_hash:
                raise PolicyError("task body changed after claim")
            if not requirements:
                raise ProtocolError("revision requires at least one requirement")
            if not branch or not task.get("worktree"):
                raise RuntimeError("revision has no retained branch or worktree")
            worktree = Path(str(task["worktree"]))
            if not worktree.is_dir():
                raise RuntimeError("revision worktree is missing")
            if self.git.current_branch(worktree) != branch:
                raise PolicyError("revision worktree is on an unexpected branch")
            baseline_head = self.git.current_head(worktree)
            if self.git.diff_summary(worktree, baseline_head).changed_paths:
                raise PolicyError("revision worktree has uncommitted changes")

            project_config = load_project_config(worktree / ".codex-worker/project.toml")
            self._validate_project_worker_app(project_config)
            self._validate_runtime_policy(worktree)
            validate_task_policy(spec, project_config)
            self._validate_context_files(worktree, spec.context_files)
            revision_started = datetime.now(UTC)
            hard_deadline = revision_started + timedelta(
                minutes=project_config.task_hard_timeout_minutes
            )
            remaining = (hard_deadline - datetime.now(UTC)).total_seconds()
            monitor = self._command_monitor(repo, number)
            preparation_result = run_commands(
                worktree,
                project_config.preparation,
                timeout_seconds=max(1, min(remaining, 1800)),
                codex_path=self.config.codex_path if self.config.codex_home else None,
                codex_home=self.config.codex_home,
                permission_profile="codex-worker-preparation",
                control_callback=monitor,
            )
            if preparation_result.termination_reason:
                state = (
                    "cancelled"
                    if preparation_result.termination_reason == "cancel"
                    else "paused"
                )
                self.store.upsert_task(
                    repo=repo,
                    issue_number=number,
                    task_hash=task_hash,
                    state=state,
                    branch=branch,
                    worktree=str(worktree),
                    pr_number=int(task["pr_number"]),
                )
                self._set_state(repo, issue, "cancelled" if state == "cancelled" else "claimed")
                return
            if not preparation_result.passed:
                raise RuntimeError(
                    "preparation failed:\n" + self._verification_detail(preparation_result)
                )

            self._set_state(repo, issue, "running")
            self.store.upsert_task(
                repo=repo,
                issue_number=number,
                task_hash=task_hash,
                state="running",
                branch=branch,
                worktree=str(worktree),
                pr_number=int(task["pr_number"]),
            )
            progress_at = iso_now()
            status_state = "running"
            hard_deadline_at = hard_deadline.isoformat()
            status = self.github.add_comment(
                repo,
                number,
                self._status_body(
                    task_hash=task_hash,
                    state="running",
                    branch=branch,
                    progress_at=progress_at,
                    detail="Authorized revision session",
                    hard_deadline_at=hard_deadline_at,
                ),
            )
            status_comment_id = int(status["id"])
            self.config.output_root.mkdir(parents=True, exist_ok=True)
            schema_path = self.config.output_root / "result.schema.json"
            schema_path.write_text(
                json.dumps(result_schema(spec), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            def heartbeat() -> None:
                self.github.update_comment(
                    repo,
                    status_comment_id,
                    self._status_body(
                        task_hash=task_hash,
                        state=status_state,
                        branch=branch,
                        progress_at=progress_at,
                        detail="Authorized revision session",
                        hard_deadline_at=hard_deadline_at,
                    ),
                )

            prompt = build_revision_prompt(
                spec,
                "\n".join(f"- {item}" for item in requirements),
                self.git.diff_stat(worktree, spec.context_commit),
            )
            runner_result = None
            verification_result = None
            structured_result = None
            for attempt in range(1, project_config.max_automatic_attempts + 1):
                remaining = (hard_deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise RunnerTimeout("revision hard timeout exceeded")
                run_id = self.store.start_run(repo, number)
                runner_result = self.runner.run(
                    worktree,
                    prompt,
                    schema_path,
                    timeout_seconds=min(
                        project_config.codex_attempt_timeout_minutes * 60,
                        remaining,
                    ),
                    heartbeat_callback=heartbeat,
                    heartbeat_interval_seconds=self.config.heartbeat_seconds,
                    control_callback=monitor,
                )
                self.store.finish_run(
                    run_id,
                    exit_code=runner_result.exit_code,
                    result={
                        "session_id": runner_result.session_id,
                        "termination_reason": runner_result.termination_reason,
                        "event_count": len(runner_result.events),
                        "last_message": runner_result.last_message[:8000],
                        "model": runner_result.model,
                        "cli_version": runner_result.cli_version,
                    },
                )
                if runner_result.termination_reason == "cancel":
                    self.store.upsert_task(
                        repo=repo,
                        issue_number=number,
                        task_hash=task_hash,
                        state="cancelled",
                        branch=branch,
                        worktree=str(worktree),
                        session_id=runner_result.session_id,
                        pr_number=int(task["pr_number"]),
                    )
                    self._set_state(repo, issue, "cancelled")
                    return
                if runner_result.termination_reason == "pause":
                    self.store.upsert_task(
                        repo=repo,
                        issue_number=number,
                        task_hash=task_hash,
                        state="paused",
                        branch=branch,
                        worktree=str(worktree),
                        session_id=runner_result.session_id,
                        pr_number=int(task["pr_number"]),
                    )
                    self._set_state(repo, issue, "claimed")
                    return
                if runner_result.exit_code != 0:
                    if attempt == project_config.max_automatic_attempts:
                        raise RuntimeError(
                            f"Codex exited {runner_result.exit_code}: {runner_result.stderr}"
                        )
                    status_state = "retrying"
                    progress_at = iso_now()
                    self._set_state(repo, issue, "retrying")
                    self.store.upsert_task(
                        repo=repo,
                        issue_number=number,
                        task_hash=task_hash,
                        state="retrying",
                        branch=branch,
                        worktree=str(worktree),
                        session_id=runner_result.session_id,
                        pr_number=int(task["pr_number"]),
                    )
                    heartbeat()
                    continue

                structured_result = self._require_completed_result(runner_result, spec)
                self._validate_delivery_diff(worktree, baseline_head, spec, project_config)
                self._set_state(repo, issue, "verifying")
                status_state = "verifying"
                progress_at = iso_now()
                self.store.upsert_task(
                    repo=repo,
                    issue_number=number,
                    task_hash=task_hash,
                    state="verifying",
                    branch=branch,
                    worktree=str(worktree),
                    session_id=runner_result.session_id,
                    pr_number=int(task["pr_number"]),
                )
                heartbeat()
                remaining = (hard_deadline - datetime.now(UTC)).total_seconds()
                if remaining <= 0:
                    raise RunnerTimeout("revision hard timeout exceeded before verification")
                verification_result = run_verification(
                    worktree,
                    project_config,
                    spec.verification_profile,
                    timeout_seconds=max(1, min(remaining, 1800)),
                    codex_path=self.config.codex_path if self.config.codex_home else None,
                    codex_home=self.config.codex_home,
                    control_callback=monitor,
                )
                if verification_result.termination_reason:
                    state = (
                        "cancelled"
                        if verification_result.termination_reason == "cancel"
                        else "paused"
                    )
                    self.store.upsert_task(
                        repo=repo,
                        issue_number=number,
                        task_hash=task_hash,
                        state=state,
                        branch=branch,
                        worktree=str(worktree),
                        session_id=runner_result.session_id,
                        pr_number=int(task["pr_number"]),
                    )
                    self._set_state(
                        repo, issue, "cancelled" if state == "cancelled" else "claimed"
                    )
                    return
                if verification_result.passed:
                    break
                if attempt == project_config.max_automatic_attempts:
                    raise RuntimeError(
                        "verification failed:\n" + self._verification_detail(verification_result)
                    )
                status_state = "retrying"
                progress_at = iso_now()
                self._set_state(repo, issue, "retrying")
                self.store.upsert_task(
                    repo=repo,
                    issue_number=number,
                    task_hash=task_hash,
                    state="retrying",
                    branch=branch,
                    worktree=str(worktree),
                    session_id=runner_result.session_id,
                    pr_number=int(task["pr_number"]),
                )
                heartbeat()
                prompt = build_revision_prompt(
                    spec,
                    "Fix only the verification failures shown below.",
                    self._verification_detail(verification_result),
                )

            assert (
                runner_result is not None
                and verification_result is not None
                and structured_result is not None
            )
            latest_issue = self.github.get_issue(repo, number)
            if parse_task_body(str(latest_issue.get("body", ""))).task_hash != task_hash:
                raise PolicyError("task body changed during revision")
            self._validate_delivery_diff(worktree, baseline_head, spec, project_config)
            self.validate_repository_authority(repository)
            commit_sha = self.git.commit(
                worktree,
                f"fix: revise codex task #{number}",
                author_name="Codex Mac Worker",
                author_email="codex-worker@users.noreply.github.com",
            )
            self.git.push(
                worktree,
                branch=branch,
                clone_url=repository.clone_url,
                token=self.token_provider(),
            )
            self.github.update_pull_request(
                repo,
                int(task["pr_number"]),
                body=self._delivery_pr_body(
                    issue_number=number,
                    spec=spec,
                    task_hash=task_hash,
                    commit_sha=commit_sha,
                    runner_result=runner_result,
                    structured_result=structured_result,
                    verification_result=verification_result,
                ),
            )
            self.store.upsert_task(
                repo=repo,
                issue_number=number,
                task_hash=task_hash,
                state="awaiting-review",
                branch=branch,
                worktree=str(worktree),
                session_id=runner_result.session_id,
                pr_number=int(task["pr_number"]),
            )
            self._set_state(repo, issue, "awaiting-review")
            self.github.update_comment(
                repo,
                status_comment_id,
                self._status_body(
                    task_hash=task_hash,
                    state="awaiting-review",
                    branch=branch,
                    progress_at=iso_now(),
                    detail=f"Revision commit {commit_sha}",
                ),
            )
        except Exception as exc:
            self._mark_attention(repo, issue, task_hash, branch, f"{type(exc).__name__}: {exc}")
