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


class GitOperations:
    def __init__(
        self,
        *,
        cache_root: Path,
        worktree_root: Path,
        git_path: str = "git",
        network_retry_delays: tuple[float, ...] = (1.0, 3.0),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cache_root = cache_root
        self.worktree_root = worktree_root
        self.git_path = git_path
        self.network_retry_delays = network_retry_delays
        self._sleep = sleep

    def _git(
        self,
        cwd: Path,
        *args: str,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        result = subprocess.run(
            [self.git_path, *args],
            cwd=cwd,
            env=merged_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if check and result.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    @staticmethod
    def _is_retryable_network_failure(stderr: str) -> bool:
        normalized = stderr.casefold()
        transient_markers = (
            "ssl connection timeout",
            "connection timed out",
            "operation timed out",
            "could not resolve host",
            "couldn't resolve host",
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

    def _git_network(
        self,
        cwd: Path,
        *args: str,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        attempts = len(self.network_retry_delays) + 1
        for attempt in range(attempts):
            result = self._git(cwd, *args, env=env, check=False)
            if result.returncode == 0:
                return result
            retryable = self._is_retryable_network_failure(result.stderr)
            if not retryable or attempt == attempts - 1:
                attempt_detail = f" after {attempt + 1} attempts" if retryable else ""
                raise GitError(
                    f"git {' '.join(args)} failed{attempt_detail}: {result.stderr.strip()}",
                    retryable=retryable,
                )
            self._sleep(self.network_retry_delays[attempt])
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
                self._git_network(mirror, "remote", "update", "--prune", env=env)
                return mirror
            mirror.parent.mkdir(parents=True, exist_ok=True)
            self._git_network(
                mirror.parent,
                "clone",
                "--mirror",
                clone_url,
                str(mirror),
                env=env,
            )
        return mirror

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

    def push(
        self,
        worktree: Path,
        *,
        branch: str,
        clone_url: str,
        token: str | None,
    ) -> None:
        remote_name = "codex-worker-delivery"
        existing = self._git(worktree, "remote", "get-url", remote_name, check=False)
        if existing.returncode == 0:
            self._git(worktree, "remote", "set-url", remote_name, clone_url)
        else:
            self._git(worktree, "remote", "add", remote_name, clone_url)
        with self._authentication(token) as env:
            self._git_network(
                worktree,
                "push",
                remote_name,
                f"HEAD:refs/heads/{branch}",
                env=env,
            )
