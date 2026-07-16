from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Callable, Iterator, Mapping

from .coordination import paths_overlap


_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
_PROXY_BYPASS_ENV_KEYS = ("NO_PROXY", "no_proxy")


class GitError(RuntimeError):
    """Raised when repository preparation or integrity validation fails."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class PreparedWorktree:
    path: Path
    branch: str
    baseline_head: str


@dataclass(frozen=True, slots=True)
class DiffSummary:
    changed_paths: tuple[str, ...]
    diff_lines: int


@dataclass(frozen=True, slots=True)
class IntegrationResult:
    task_commit: str
    integrated_base: str
    delivery_head: str
    refresh_count: int


class GitOperations:
    def __init__(
        self,
        *,
        cache_root: Path,
        worktree_root: Path,
        git_path: str = "git",
        proxy_url: str | None = None,
        network_retry_delays: tuple[float, ...] = (1.0, 3.0),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache_root = cache_root
        self.worktree_root = worktree_root
        self.git_path = git_path
        self.proxy_url = proxy_url
        self.network_retry_delays = network_retry_delays
        self._sleep = sleep

    def _git(
        self,
        cwd: Path,
        *args: str,
        env: Mapping[str, str | None] | None = None,
        check: bool = True,
        timeout_seconds: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        if env:
            for key, value in env.items():
                if value is None:
                    merged_env.pop(key, None)
                else:
                    merged_env[key] = value
        try:
            result = subprocess.run(
                [self.git_path, *args],
                cwd=cwd,
                env=merged_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            result = subprocess.CompletedProcess(
                [self.git_path, *args],
                124,
                stdout,
                (stderr + "\ngit delivery deadline expired").strip(),
            )
        if check and result.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    @staticmethod
    def _is_retryable_network_failure(stderr: str) -> bool:
        normalized = stderr.casefold()
        permanent_markers = (
            "authentication failed",
            "permission denied",
            "access denied",
            "repository not found",
            "could not read from remote repository",
            "ssl certificate problem",
            "certificate verify failed",
            "server certificate verification failed",
            "self-signed certificate",
            "refusing to fetch into branch checked out",
            "not a git repository",
            "does not appear to be a git repository",
            "couldn't find remote ref",
            "non-fast-forward",
            "remote rejected",
            "cannot lock ref",
            "unable to update local ref",
            "would clobber existing tag",
        )
        if any(marker in normalized for marker in permanent_markers) or re.search(
            r"requested url returned error:\s*(?:400|401|403|404)\b",
            normalized,
        ):
            return False
        transient_markers = (
            "ssl connection timeout",
            "connection timed out",
            "operation timed out",
            "could not resolve host",
            "couldn't resolve host",
            "could not resolve proxy",
            "couldn't resolve proxy",
            "failed to connect to",
            "connection reset",
            "remote end hung up unexpectedly",
            "early eof",
            "ssl_error_syscall",
            "send failure: broken pipe",
            "error in the http2 framing layer",
            "http/2 stream",
            "gnutls recv error",
            "tls connection was non-properly terminated",
            "unexpected disconnect while reading sideband packet",
            "empty reply from server",
        )
        if any(marker in normalized for marker in transient_markers):
            return True
        return re.search(
            r"requested url returned error:\s*(?:429|5\d\d)\b",
            normalized,
        ) is not None

    def _proxy_environment(
        self,
        *,
        use_proxy: bool,
        target_url: str,
    ) -> dict[str, str | None]:
        value = self.proxy_url if use_proxy else None
        environment = {key: value for key in _PROXY_ENV_KEYS}
        environment.update({key: None for key in _PROXY_BYPASS_ENV_KEYS})

        git_proxy_value = self.proxy_url if use_proxy else ""
        normalized_target = target_url.rstrip("/")
        config_entries = (
            ("http.proxy", git_proxy_value),
            (f"http.{normalized_target}.proxy", git_proxy_value),
            (f"http.{normalized_target}/.proxy", git_proxy_value),
        )
        environment["GIT_CONFIG_PARAMETERS"] = None
        environment["GIT_CONFIG_COUNT"] = str(len(config_entries))
        for index, (key, config_value) in enumerate(config_entries):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = config_value
        return environment

    def _git_network(
        self,
        cwd: Path,
        *args: str,
        env: Mapping[str, str | None] | None = None,
        proxy_target_url: str | None = None,
        deadline_monotonic: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        attempts = len(self.network_retry_delays) + 1
        for attempt in range(attempts):
            attempt_env: dict[str, str | None] = dict(env or {})
            if self.proxy_url is not None and proxy_target_url is not None:
                attempt_env.update(
                    self._proxy_environment(
                        use_proxy=attempt % 2 == 0,
                        target_url=proxy_target_url,
                    )
                )
            call_options: dict[str, float] = {}
            if deadline_monotonic is not None:
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    raise GitError("git delivery deadline expired", retryable=True)
                call_options["timeout_seconds"] = remaining
            result = self._git(
                cwd,
                *args,
                env=attempt_env,
                check=False,
                **call_options,
            )
            if result.returncode == 0:
                return result
            retryable = result.returncode == 124 or self._is_retryable_network_failure(
                result.stderr
            )
            if not retryable or attempt == attempts - 1:
                attempt_detail = f" after {attempt + 1} attempts" if retryable else ""
                raise GitError(
                    f"git {' '.join(args)} failed{attempt_detail}: {result.stderr.strip()}",
                    retryable=retryable,
                )
            delay = self.network_retry_delays[attempt]
            if (
                deadline_monotonic is not None
                and time.monotonic() + delay >= deadline_monotonic
            ):
                raise GitError("git delivery deadline expired", retryable=True)
            self._sleep(delay)
        raise AssertionError("unreachable")

    @contextmanager
    def _authentication(self, token: str | None) -> Iterator[dict[str, str]]:
        if token is None:
            yield {}
            return
        self.cache_root.mkdir(parents=True, exist_ok=True)
        descriptor, askpass_name = tempfile.mkstemp(prefix="askpass-", dir=self.cache_root)
        os.close(descriptor)
        askpass = Path(askpass_name)
        try:
            askpass.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *Username*) printf '%s' 'x-access-token' ;;\n"
                "  *) printf '%s' \"$CODEX_WORKER_GIT_TOKEN\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            askpass.chmod(0o700)
            yield {
                "GIT_ASKPASS": str(askpass),
                "GIT_TERMINAL_PROMPT": "0",
                "CODEX_WORKER_GIT_TOKEN": token,
            }
        finally:
            askpass.unlink(missing_ok=True)

    def ensure_mirror(self, repo: str, clone_url: str, token: str | None = None) -> Path:
        owner, name = repo.split("/", 1)
        mirror = self.cache_root / owner / f"{name}.git"
        with self._authentication(token) as env:
            if mirror.exists():
                self._git_network(
                    mirror,
                    "remote",
                    "update",
                    "--prune",
                    env=env,
                    proxy_target_url=clone_url,
                )
                return mirror
            mirror.parent.mkdir(parents=True, exist_ok=True)
            self._git_network(
                mirror.parent,
                "clone",
                "--mirror",
                clone_url,
                str(mirror),
                env=env,
                proxy_target_url=clone_url,
            )
        return mirror

    def refresh_branch(
        self,
        mirror: Path,
        *,
        clone_url: str,
        branch: str,
        token: str | None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or branch.startswith(("/", "-")):
            raise GitError("invalid branch name for mirror refresh")
        with self._authentication(token) as env:
            self._git_network(
                mirror,
                "fetch",
                "--no-tags",
                "origin",
                f"+refs/heads/{branch}:refs/heads/{branch}",
                env=env,
                proxy_target_url=clone_url,
            )

    def prepare_worktree(
        self,
        *,
        repo: str,
        mirror: Path,
        context_commit: str,
        base_branch: str,
        issue_number: int,
        slug: str,
    ) -> PreparedWorktree:
        available = self._git(
            mirror,
            "cat-file",
            "-e",
            f"{context_commit}^{{commit}}",
            check=False,
        )
        if available.returncode != 0:
            raise GitError("context commit is not available in repository")
        ancestor = self._git(
            mirror,
            "merge-base",
            "--is-ancestor",
            context_commit,
            f"refs/heads/{base_branch}",
            check=False,
        )
        if ancestor.returncode != 0:
            raise GitError("context commit is not an ancestor of the base branch")

        safe_slug = re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-")[:48] or "task"
        branch = f"codex/{issue_number}-{safe_slug}"
        owner, name = repo.split("/", 1)
        path = self.worktree_root / owner / name / f"{issue_number}-{safe_slug}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            head = self._git(path, "rev-parse", "HEAD").stdout.strip()
            if head != context_commit:
                raise GitError("existing worktree HEAD does not match context commit")
            return PreparedWorktree(path=path, branch=branch, baseline_head=context_commit)
        branch_exists = self._git(
            mirror,
            "show-ref",
            "--verify",
            f"refs/heads/{branch}",
            check=False,
        ).returncode == 0
        args = ["worktree", "add"]
        if branch_exists:
            args.extend([str(path), branch])
        else:
            args.extend(["-b", branch, str(path), context_commit])
        self._git(mirror, *args)
        head = self._git(path, "rev-parse", "HEAD").stdout.strip()
        if head != context_commit:
            raise GitError("prepared worktree does not match context commit")
        return PreparedWorktree(path=path, branch=branch, baseline_head=head)

    def assert_head_unchanged(self, worktree: Path, baseline_head: str) -> None:
        current = self.current_head(worktree)
        if current != baseline_head:
            raise GitError(f"worktree HEAD changed from {baseline_head} to {current}")

    def current_head(self, worktree: Path) -> str:
        return self._git(worktree, "rev-parse", "HEAD").stdout.strip()

    def current_branch(self, worktree: Path) -> str:
        return self._git(worktree, "branch", "--show-current").stdout.strip()

    def is_clean(self, worktree: Path) -> bool:
        return not self._git(
            worktree,
            "status",
            "--porcelain",
            "--untracked-files=all",
        ).stdout

    def commit_parents(self, worktree: Path, commit_sha: str) -> tuple[str, ...]:
        fields = self._git(
            worktree,
            "rev-list",
            "--parents",
            "-n",
            "1",
            commit_sha,
        ).stdout.strip().split()
        if not fields or fields[0] != commit_sha:
            raise GitError("delivery commit cannot be resolved")
        return tuple(fields[1:])

    def diff_stat(self, worktree: Path, baseline_head: str) -> str:
        return self._git(worktree, "diff", "--stat", f"{baseline_head}..HEAD", "--").stdout.strip()

    def diff_summary(self, worktree: Path, baseline_head: str) -> DiffSummary:
        numstat = self._git(worktree, "diff", "--numstat", baseline_head, "--").stdout
        paths: set[str] = set()
        diff_lines = 0
        for line in numstat.splitlines():
            added, deleted, path = line.split("\t", 2)
            paths.add(path)
            if added.isdigit():
                diff_lines += int(added)
            if deleted.isdigit():
                diff_lines += int(deleted)
        untracked = self._git(
            worktree,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).stdout
        for raw_path in untracked.split("\0"):
            if not raw_path:
                continue
            paths.add(raw_path)
            file_path = worktree / raw_path
            try:
                with file_path.open("rb") as handle:
                    diff_lines += sum(1 for _ in handle)
            except (OSError, UnicodeError):
                pass
        return DiffSummary(changed_paths=tuple(sorted(paths)), diff_lines=diff_lines)

    def commit(self, worktree: Path, message: str, *, author_name: str, author_email: str) -> str:
        self._git(worktree, "add", "--all")
        env = {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
        self._git(worktree, "commit", "-m", message, env=env)
        return self._git(worktree, "rev-parse", "HEAD").stdout.strip()

    def changed_paths_between(
        self,
        repository: Path,
        base: str,
        head: str,
    ) -> tuple[str, ...]:
        raw = self._git(
            repository,
            "diff",
            "--name-status",
            "--find-renames",
            "-z",
            f"{base}..{head}",
            "--",
        ).stdout
        fields = raw.split("\0")
        paths: list[str] = []
        index = 0
        while index < len(fields) and fields[index]:
            status = fields[index]
            index += 1
            if index >= len(fields):
                raise GitError("unable to parse changed paths")
            if status.startswith(("R", "C")):
                if index + 1 >= len(fields):
                    raise GitError("unable to parse renamed paths")
                paths.extend((fields[index], fields[index + 1]))
                index += 2
            else:
                paths.append(fields[index])
                index += 1
        return tuple(dict.fromkeys(paths))

    def integrate_default(
        self,
        worktree: Path,
        mirror: Path,
        base_branch: str,
        integrated_base: str,
        task_paths: tuple[str, ...],
        *,
        refresh_count: int = 0,
        author_name: str,
        author_email: str,
    ) -> IntegrationResult:
        task_commit = self.current_head(worktree)
        current_base = self._git(
            mirror,
            "rev-parse",
            f"refs/heads/{base_branch}",
        ).stdout.strip()
        if current_base == integrated_base:
            return IntegrationResult(
                task_commit=task_commit,
                integrated_base=integrated_base,
                delivery_head=task_commit,
                refresh_count=refresh_count,
            )
        if refresh_count >= 2:
            raise GitError("default branch advanced more than two times")
        ancestor = self._git(
            mirror,
            "merge-base",
            "--is-ancestor",
            integrated_base,
            current_base,
            check=False,
        )
        if ancestor.returncode != 0:
            raise GitError("current default branch is not a descendant of the integrated base")
        main_paths = self.changed_paths_between(mirror, integrated_base, current_base)
        if paths_overlap(task_paths, main_paths):
            raise GitError("default branch changes overlap task paths")
        if not self.is_clean(worktree):
            raise GitError("worktree must be clean before default-branch integration")

        environment = {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_COMMITTER_NAME": author_name,
            "GIT_COMMITTER_EMAIL": author_email,
        }
        merged = self._git(
            worktree,
            "merge",
            "--no-ff",
            "--no-edit",
            current_base,
            env=environment,
            check=False,
        )
        if merged.returncode != 0:
            self._git(worktree, "merge", "--abort", check=False)
            if self.current_head(worktree) != task_commit or not self.is_clean(worktree):
                raise GitError("default-branch integration merge cleanup failed")
            raise GitError(
                "default-branch integration merge failed: " + merged.stderr.strip()
            )
        delivery_head = self.current_head(worktree)
        return IntegrationResult(
            task_commit=task_commit,
            integrated_base=current_base,
            delivery_head=delivery_head,
            refresh_count=refresh_count + 1,
        )

    def push(
        self,
        worktree: Path,
        *,
        branch: str,
        clone_url: str,
        token: str | None,
        deadline_monotonic: float | None = None,
    ) -> None:
        remote_name = "codex-worker-delivery"
        existing = self._git(worktree, "remote", "get-url", remote_name, check=False)
        if existing.returncode == 0:
            self._git(worktree, "remote", "set-url", remote_name, clone_url)
        else:
            self._git(worktree, "remote", "add", remote_name, clone_url)
        with self._authentication(token) as env:
            deadline_options: dict[str, float] = {}
            if deadline_monotonic is not None:
                deadline_options["deadline_monotonic"] = deadline_monotonic
            self._git_network(
                worktree,
                "push",
                remote_name,
                f"HEAD:refs/heads/{branch}",
                env=env,
                proxy_target_url=clone_url,
                **deadline_options,
            )
