from __future__ import annotations

import fcntl
from pathlib import Path
import signal
import time
from typing import Any, Protocol

from .config import ConfigError, RepositoryConfig, WorkerConfig, parse_project_config
from .protocol import (
    REPOSITORY_PROBE_MARKER,
    ProtocolError,
    parse_command_comment,
)
from .store import EventStore


class AlreadyRunning(RuntimeError):
    """Raised when another Worker owns the host-level instance lock."""


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def __enter__(self) -> SingleInstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise AlreadyRunning(f"another Worker holds {self.path}") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


class QueueGitHub(Protocol):
    def list_queued_issues(self, repo: str) -> list[dict[str, Any]]: ...


class IssueProcessor(Protocol):
    def process_repository_probe(
        self,
        repository: RepositoryConfig,
        issue: dict[str, Any],
    ) -> None: ...

    def process_issue(
        self,
        repository: RepositoryConfig,
        issue: dict[str, Any],
        resume_session_id: str | None = None,
    ) -> None: ...

    def revise_issue(
        self,
        repository: RepositoryConfig,
        issue: dict[str, Any],
        task: dict[str, Any],
        requirements: tuple[str, ...],
    ) -> None: ...

    def retry_delivery(
        self,
        repository: RepositoryConfig,
        issue: dict[str, Any],
    ) -> str: ...

    def retry_execution_delivery(
        self,
        repository: RepositoryConfig,
        issue: dict[str, Any],
    ) -> str: ...

    def auto_merge_delivery(
        self,
        repository: RepositoryConfig,
        issue: dict[str, Any],
        task: dict[str, Any],
    ) -> str: ...


class WorkerDaemon:
    """Single-worker polling loop; never claims a second task while one is active."""

    def __init__(
        self,
        config: WorkerConfig,
        github: QueueGitHub,
        store: EventStore,
        service: IssueProcessor,
    ) -> None:
        self.config = config
        self.github = github
        self.store = store
        self.service = service
        self._stopping = False

    def stop(self, *_: object) -> None:
        self._stopping = True
        stop_service = getattr(self.service, "stop", None)
        if stop_service is not None:
            stop_service()

    def _repository(self, name: str) -> RepositoryConfig:
        for repository in self.repositories():
            if repository.name == name:
                return repository
        raise KeyError(f"repository is no longer configured: {name}")

    def repositories(self) -> tuple[RepositoryConfig, ...]:
        configured = {item.name: item for item in self.config.repositories}
        if not self.config.discover_installation_repositories:
            return tuple(sorted(configured.values(), key=lambda item: item.name))

        cache_key = "repository_discovery:v1"
        cached = self.store.get_worker_state(cache_key, {})
        now = time.time()
        if isinstance(cached, dict) and now - float(cached.get("refreshed_at", 0)) < 300:
            for item in cached.get("repositories", []):
                if isinstance(item, dict) and item.get("name") and item.get("clone_url"):
                    repository = RepositoryConfig(str(item["name"]), str(item["clone_url"]))
                    configured.setdefault(repository.name, repository)
            return tuple(sorted(configured.values(), key=lambda item: item.name))

        discovered: list[RepositoryConfig] = []
        try:
            installations = self.github.list_installation_repositories()  # type: ignore[attr-defined]
            for item in installations:
                name = item.get("full_name")
                clone_url = item.get("clone_url")
                default_branch = item.get("default_branch")
                if not all(isinstance(value, str) and value for value in (name, clone_url, default_branch)):
                    continue
                try:
                    text = self.github.get_repository_file(  # type: ignore[attr-defined]
                        name,
                        ".codex-worker/project.toml",
                        ref=default_branch,
                    )
                except Exception as exc:
                    if getattr(exc, "status_code", None) == 404:
                        continue
                    raise
                try:
                    project = parse_project_config(text)
                except ConfigError:
                    continue
                if project.default_base_branch != default_branch:
                    continue
                if project.worker_github_app_id != int(self.config.github_app_id):
                    continue
                discovered.append(RepositoryConfig(name, clone_url))
        except Exception:
            if not isinstance(cached, dict) or not cached.get("repositories"):
                raise
            discovered = [
                RepositoryConfig(str(item["name"]), str(item["clone_url"]))
                for item in cached["repositories"]
                if isinstance(item, dict) and item.get("name") and item.get("clone_url")
            ]
        else:
            self.store.set_worker_state(
                cache_key,
                {
                    "refreshed_at": now,
                    "repositories": [
                        {"name": item.name, "clone_url": item.clone_url}
                        for item in discovered
                    ],
                },
            )

        for repository in discovered:
            configured.setdefault(repository.name, repository)
        return tuple(sorted(configured.values(), key=lambda item: item.name))

    def _set_remote_state(self, repo: str, issue: dict[str, Any], state: str) -> None:
        labels = []
        for item in issue.get("labels", []):
            value = item.get("name") if isinstance(item, dict) else item
            if isinstance(value, str) and not value.startswith("codex:"):
                labels.append(value)
        labels.append(f"codex:{state}")
        self.github.set_labels(repo, int(issue["number"]), labels)  # type: ignore[attr-defined]

    def _record_repository_eligibility(
        self, repo: str, value: dict[str, Any]
    ) -> None:
        key = f"repository_eligibility:{repo}"
        if self.store.get_worker_state(key) != value:
            self.store.set_worker_state(key, value)

    def recover_active_tasks(self) -> bool:
        recovered = False
        recoverable = {"claimed", "running", "verifying", "retrying"}
        for task in self.store.active_tasks():
            if task["state"] not in recoverable:
                continue
            key = f"recovery:{task['repo']}#{task['issue_number']}:{task['task_hash']}"
            if self.store.get_worker_state(key, 0) >= 1:
                continue
            self.store.set_worker_state(key, 1)
            issue = self.github.get_issue(task["repo"], task["issue_number"])  # type: ignore[attr-defined]
            self.store.upsert_task(
                repo=str(task["repo"]),
                issue_number=int(task["issue_number"]),
                task_hash=str(task["task_hash"]),
                state="needs-attention",
                branch=task.get("branch"),
                worktree=task.get("worktree"),
                session_id=task.get("session_id"),
                pr_number=task.get("pr_number"),
            )
            self._set_remote_state(str(task["repo"]), issue, "needs-attention")
            recovered = True
        return recovered

    def _next_control_command(
        self,
        repo: str,
        issue_number: int,
    ) -> dict[str, Any] | None:
        for command in self.store.pending_commands(repo, issue_number):
            action = str(command["action"])
            author = str(command["author"])
            if action not in {"resume", "retry", "cancel"}:
                continue
            permission = self.github.collaborator_permission(repo, author)  # type: ignore[attr-defined]
            if author not in self.config.authorized_users or permission not in {
                "admin",
                "maintain",
                "write",
            }:
                self.store.mark_command_executed(
                    str(command["command_id"]),
                    "authorization-revoked",
                )
                continue
            return command

        for comment in reversed(self.github.list_comments(repo, issue_number)):  # type: ignore[attr-defined]
            body = str(comment.get("body", ""))
            if "<!-- codex-command:v1 -->" not in body:
                continue
            try:
                parsed = parse_command_comment(body)
            except ProtocolError:
                continue
            if parsed.issue_number != issue_number or parsed.action not in {
                "resume",
                "retry",
                "cancel",
            }:
                continue
            author = str(comment.get("user", {}).get("login", ""))
            permission = self.github.collaborator_permission(repo, author)  # type: ignore[attr-defined]
            if author not in self.config.authorized_users or permission not in {
                "admin",
                "maintain",
                "write",
            }:
                continue
            self.store.record_command(
                parsed.command_id,
                repo,
                issue_number,
                parsed.action,
                author,
            )
            command = self.store.get_command(parsed.command_id)
            if command is None or command.get("executed_at") is not None:
                continue
            if (
                command["repo"] != repo
                or int(command["issue_number"]) != issue_number
                or command["action"] != parsed.action
                or command["author"] != author
            ):
                continue
            return command
        return None

    def process_control_commands(self) -> bool:
        tasks = self.store.tasks_in_states(("paused", "needs-attention"))
        for task in tasks:
            repo = task["repo"]
            issue_number = int(task["issue_number"])
            issue = self.github.get_issue(repo, issue_number)  # type: ignore[attr-defined]
            command = self._next_control_command(repo, issue_number)
            if command is None:
                continue
            command_id = str(command["command_id"])
            action = str(command["action"])
            if action == "cancel":
                self.store.upsert_task(
                    repo=repo,
                    issue_number=issue_number,
                    task_hash=task["task_hash"],
                    state="cancelled",
                    branch=task["branch"],
                    worktree=task["worktree"],
                )
                self._set_remote_state(repo, issue, "cancelled")
                self.store.mark_command_executed(command_id, "cancel")
                return True
            if action == "retry":
                checkpoint = self.store.get_delivery_checkpoint(
                    repo, issue_number, str(task["task_hash"])
                )
                pr_number = task.get("pr_number")
                legacy_completed_delivery = (
                    checkpoint is not None
                    and checkpoint.get("phase") == "validation"
                    and checkpoint.get("retryable") is False
                    and checkpoint.get("last_error")
                    == "PolicyError: delivery checkpoint is not retryable"
                    and self.store.has_executed_command_result(
                        repo,
                        issue_number,
                        ("awaiting-review", "merging"),
                    )
                )
                completed_automatic_delivery = (
                    self.config.merge_mode == "automatic"
                    and task.get("state") == "needs-attention"
                    and isinstance(pr_number, int)
                    and not isinstance(pr_number, bool)
                    and checkpoint is not None
                    and checkpoint.get("retryable") is False
                    and (
                        checkpoint.get("phase") == "complete"
                        or legacy_completed_delivery
                    )
                )
                if completed_automatic_delivery:
                    self.store.upsert_task(
                        repo=repo,
                        issue_number=issue_number,
                        task_hash=task["task_hash"],
                        state="merging",
                        branch=task["branch"],
                        worktree=task["worktree"],
                        session_id=task.get("session_id"),
                        pr_number=pr_number,
                    )
                    self._set_remote_state(repo, issue, "merging")
                    self.store.mark_command_executed(command_id, "merging")
                    return True
                successful_execution = any(
                    run.get("finished_at")
                    and run.get("exit_code") == 0
                    and isinstance(run.get("result"), dict)
                    and run["result"].get("termination_reason") is None
                    and isinstance(run["result"].get("session_id"), str)
                    and bool(run["result"]["session_id"])
                    for run in self.store.list_runs(repo, issue_number)
                )
                post_execution_failure = (
                    checkpoint is None
                    and task.get("pr_number") is None
                    and bool(task.get("worktree"))
                    and successful_execution
                )
                if post_execution_failure:
                    result = self.service.retry_execution_delivery(
                        self._repository(repo), issue
                    )
                    self.store.mark_command_executed(command_id, result)
                    return True
                pre_execution_failure = (
                    checkpoint is None
                    and not task.get("worktree")
                    and not task.get("session_id")
                    and not self.store.list_runs(repo, issue_number)
                )
                if pre_execution_failure:
                    self.store.upsert_task(
                        repo=repo,
                        issue_number=issue_number,
                        task_hash=task["task_hash"],
                        state="retrying",
                        branch=task["branch"],
                    )
                    self._set_remote_state(repo, issue, "retrying")
                    self.service.process_issue(self._repository(repo), issue)
                    self.store.mark_command_executed(
                        command_id, "pre-execution-retry"
                    )
                    return True
                self.store.upsert_task(
                    repo=repo,
                    issue_number=issue_number,
                    task_hash=task["task_hash"],
                    state="retrying",
                    branch=task["branch"],
                    worktree=task["worktree"],
                )
                result = self.service.retry_delivery(self._repository(repo), issue)
                self.store.mark_command_executed(command_id, result)
                return True

            checkpoint = self.store.get_delivery_checkpoint(
                repo, issue_number, str(task["task_hash"])
            )
            if (
                checkpoint is not None
                and checkpoint.get("phase") == "paused-verification"
                and checkpoint.get("retryable") is True
            ):
                self.store.upsert_task(
                    repo=repo,
                    issue_number=issue_number,
                    task_hash=task["task_hash"],
                    state="retrying",
                    branch=task["branch"],
                    worktree=task["worktree"],
                )
                result = self.service.retry_delivery(self._repository(repo), issue)
                self.store.mark_command_executed(command_id, result)
                return True

            resume_session_id = task.get("session_id")
            resume_key = f"resume:{repo}#{issue_number}:{task['task_hash']}"
            if not resume_session_id or self.store.get_worker_state(resume_key, 0) >= 1:
                self.store.upsert_task(
                    repo=repo,
                    issue_number=issue_number,
                    task_hash=task["task_hash"],
                    state="needs-attention",
                    branch=task["branch"],
                    worktree=task["worktree"],
                )
                self._set_remote_state(repo, issue, "needs-attention")
                self.store.mark_command_executed(command_id, "resume-limit")
                return True
            self.store.set_worker_state(resume_key, 1)
            self.store.upsert_task(
                repo=repo,
                issue_number=issue_number,
                task_hash=task["task_hash"],
                state="retrying",
                branch=task["branch"],
                worktree=task["worktree"],
            )
            self.service.process_issue(
                self._repository(repo),
                issue,
                resume_session_id=str(resume_session_id),
            )
            self.store.mark_command_executed(command_id, "resume")
            return True
        return False

    def reconcile_stable_commands(self) -> bool:
        """Acknowledge commands whose durable task outcome already proves completion."""
        for task in self.store.tasks_in_states(
            ("awaiting-review", "completed", "cancelled")
        ):
            for command in self.store.pending_commands(
                str(task["repo"]), int(task["issue_number"])
            ):
                action = str(command["action"])
                state = str(task["state"])
                if action == "retry" and state in {"awaiting-review", "completed"}:
                    self.store.mark_command_executed(
                        str(command["command_id"]), state
                    )
                    return True
                if action == "cancel" and state == "cancelled":
                    self.store.mark_command_executed(
                        str(command["command_id"]), "cancelled"
                    )
                    return True
        return False

    def process_review_tasks(self) -> bool:
        """Reconcile manual reviews and trusted Worker automatic merges."""
        states = (
            ("awaiting-review", "merging")
            if self.config.merge_mode == "automatic"
            else ("awaiting-review",)
        )
        for task in self.store.tasks_in_states(states):
            repo = str(task["repo"])
            issue_number = int(task["issue_number"])
            issue = self.github.get_issue(repo, issue_number)  # type: ignore[attr-defined]
            pr_number = task.get("pr_number")
            if pr_number is not None and self.config.merge_mode != "automatic":
                pull = self.github.get_pull_request(repo, int(pr_number))  # type: ignore[attr-defined]
                if pull.get("merged_at"):
                    labels = []
                    for item in issue.get("labels", []):
                        value = item.get("name") if isinstance(item, dict) else item
                        if isinstance(value, str) and not value.startswith("codex:"):
                            labels.append(value)
                    labels.append("codex:completed")
                    self.github.update_issue(  # type: ignore[attr-defined]
                        repo,
                        issue_number,
                        labels=labels,
                        state="closed",
                    )
                    self.store.upsert_task(
                        repo=repo,
                        issue_number=issue_number,
                        task_hash=str(task["task_hash"]),
                        state="completed",
                        branch=task.get("branch"),
                        worktree=task.get("worktree"),
                        pr_number=int(pr_number),
                    )
                    return True

            if self.config.merge_mode == "automatic":
                result = self.service.auto_merge_delivery(
                    self._repository(repo), issue, task
                )
                self.store.upsert_task(
                    repo=repo,
                    issue_number=issue_number,
                    task_hash=str(task["task_hash"]),
                    state=result,
                    branch=task.get("branch"),
                    worktree=task.get("worktree"),
                    session_id=task.get("session_id"),
                    pr_number=int(pr_number) if pr_number is not None else None,
                )
                if result == "completed":
                    labels = []
                    for item in issue.get("labels", []):
                        value = item.get("name") if isinstance(item, dict) else item
                        if isinstance(value, str) and not value.startswith("codex:"):
                            labels.append(value)
                    labels.append("codex:completed")
                    self.github.update_issue(  # type: ignore[attr-defined]
                        repo,
                        issue_number,
                        labels=labels,
                        state="closed",
                    )
                elif result in {"merging", "needs-attention"}:
                    self._set_remote_state(repo, issue, result)
                else:
                    raise ValueError(f"unknown automatic merge outcome: {result}")
                return True

            for comment in reversed(self.github.list_comments(repo, issue_number)):  # type: ignore[attr-defined]
                body = str(comment.get("body", ""))
                if "<!-- codex-command:v1 -->" not in body:
                    continue
                try:
                    command = parse_command_comment(body)
                except ProtocolError:
                    continue
                if command.issue_number != issue_number or command.action != "revise":
                    continue
                author = str(comment.get("user", {}).get("login", ""))
                permission = self.github.collaborator_permission(repo, author)  # type: ignore[attr-defined]
                if author not in self.config.authorized_users or permission not in {"admin", "maintain", "write"}:
                    continue
                if not self.store.record_command(
                    command.command_id,
                    repo,
                    issue_number,
                    command.action,
                    author,
                ):
                    continue
                self.service.revise_issue(
                    self._repository(repo),
                    issue,
                    task,
                    command.requirements,
                )
                self.store.mark_command_executed(command.command_id, command.action)
                return True
        return False

    def run_once(self) -> bool:
        flush = getattr(self.github, "flush", None)
        if flush is not None:
            flush()
        if self.reconcile_stable_commands():
            return True
        if self.process_review_tasks():
            return True
        if self.process_control_commands():
            return True
        if self.store.active_tasks():
            return False
        queued: list[tuple[RepositoryConfig, dict[str, Any]]] = []
        for repository in self.repositories():
            try:
                self.service.validate_repository_authority(repository)
            except Exception as exc:
                self._record_repository_eligibility(
                    repository.name,
                    {
                        "eligible": False,
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                    },
                )
                continue
            self._record_repository_eligibility(
                repository.name,
                {"eligible": True},
            )
            for issue in self.github.list_queued_issues(repository.name):
                if "pull_request" in issue:
                    continue
                queued.append((repository, issue))
        if not queued:
            return False
        repository, issue = min(
            queued,
            key=lambda item: (str(item[1].get("created_at", "")), int(item[1]["number"])),
        )
        if REPOSITORY_PROBE_MARKER in str(issue.get("body", "")):
            self.service.process_repository_probe(repository, issue)
        else:
            self.service.process_issue(repository, issue)
        return True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.recover_active_tasks()
        while not self._stopping:
            did_work = self.run_once()
            if self._stopping:
                break
            time.sleep(1 if did_work else self.config.poll_seconds)
