from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Protocol

import yaml

from .config import RepositoryConfig, WorkerConfig, load_project_config
from .gitops import GitOperations
from .policy import PolicyError, validate_changed_paths, validate_task_policy
from .prompting import build_execution_prompt, build_revision_prompt, result_schema
from .protocol import ProtocolError, parse_command_comment, parse_task_body
from .runner import CodexRunner, RunnerResult, RunnerTimeout
from .store import EventStore
from .verification import run_commands, run_verification, scan_for_secrets


STATUS_PREFIX = "codex:"
TERMINAL_STATES = {"awaiting-review", "needs-attention", "completed", "cancelled"}
AUTHORIZED_PERMISSIONS = {"admin", "maintain", "write"}


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
    ) -> None:
        number = int(issue["number"])
        self.store.upsert_task(
            repo=repo,
            issue_number=number,
            task_hash=task_hash,
            state="needs-attention",
            branch=branch or None,
        )
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

    def _validate_issue_author(self, repo: str, issue: dict[str, Any]) -> None:
        author = str(issue.get("user", {}).get("login", ""))
        permission = self.github.collaborator_permission(repo, author) if author else "none"
        if author not in self.config.authorized_users or permission not in AUTHORIZED_PERMISSIONS:
            raise PolicyError(f"issue author {author or '<missing>'} is not authorized")

    def _require_completed_result(self, result: RunnerResult) -> None:
        try:
            payload = json.loads(result.last_message)
        except json.JSONDecodeError as exc:
            raise PolicyError("Codex returned an invalid structured result") from exc
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            raise PolicyError("Codex reported blocked instead of completed")

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

    def _verification_detail(self, result: Any) -> str:
        return "\n\n".join(
            f"$ {item.command}\nexit={item.exit_code}\n{item.output[-3000:]}"
            for item in result.commands
        )

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
                json.dumps(result_schema(), ensure_ascii=False, indent=2) + "\n",
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

                self._require_completed_result(runner_result)
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

            assert runner_result is not None and verification_result is not None
            latest_issue = self.github.get_issue(repo, number)
            if parse_task_body(str(latest_issue.get("body", ""))).task_hash != task_hash:
                raise PolicyError("task body changed after claim")

            self._validate_delivery_diff(
                prepared.path, prepared.baseline_head, spec, project_config
            )

            commit_sha = self.git.commit(
                prepared.path,
                f"feat: complete codex task #{number}",
                author_name="Codex Mac Worker",
                author_email="codex-worker@users.noreply.github.com",
            )
            self.git.push(
                prepared.path,
                branch=branch,
                clone_url=repository.clone_url,
                token=self.token_provider(),
            )
            verification_text = self._verification_detail(verification_result)
            pr_body = f"""Relates to #{number}

Context commit: `{spec.context_commit}`
Task hash: `{task_hash}`
Commit: `{commit_sha}`

## Acceptance
{chr(10).join(f'- [ ] {item}' for item in spec.acceptance)}

## Verification
```text
{verification_text}
```

This PR was created as a draft. The worker cannot merge it.
"""
            pr = self.github.create_draft_pr(
                repo,
                branch,
                spec.base_branch,
                f"[Codex #{number}] {issue.get('title', 'Task')}",
                pr_body,
            )
            self.store.upsert_task(
                repo=repo,
                issue_number=number,
                task_hash=task_hash,
                state="awaiting-review",
                branch=branch,
                worktree=str(prepared.path),
                session_id=runner_result.session_id,
                pr_number=int(pr["number"]),
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
                    detail=str(pr.get("html_url", "")),
                ),
            )
        except Exception as exc:
            self._mark_attention(repo, issue, task_hash, branch, f"{type(exc).__name__}: {exc}")

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
                json.dumps(result_schema(), ensure_ascii=False, indent=2) + "\n",
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

                self._require_completed_result(runner_result)
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

            assert runner_result is not None and verification_result is not None
            latest_issue = self.github.get_issue(repo, number)
            if parse_task_body(str(latest_issue.get("body", ""))).task_hash != task_hash:
                raise PolicyError("task body changed during revision")
            self._validate_delivery_diff(worktree, baseline_head, spec, project_config)
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
