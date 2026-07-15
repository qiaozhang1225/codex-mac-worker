from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from codex_mac_worker.gitops import GitError, GitOperations


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def make_remote(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    (source / "README.md").write_text("baseline\n", encoding="utf-8")
    git(source, "add", "README.md")
    git(source, "commit", "-m", "baseline")
    sha = git(source, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(source), str(remote))
    return remote, sha


def test_existing_mirror_retries_transient_network_failure_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delays: list[float] = []
    operations = GitOperations(
        cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
        network_retry_delays=(0.1, 0.2),
        sleep=delays.append,
    )
    mirror = tmp_path / "cache" / "owner" / "repo.git"
    mirror.mkdir(parents=True)
    attempts = 0

    def fake_git(
        cwd: Path,
        *args: str,
        env: object = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        assert cwd == mirror
        assert args == ("remote", "update", "--prune")
        assert check is False
        if attempts < 3:
            return subprocess.CompletedProcess(
                ["git", *args], 128, "", "fatal: SSL connection timeout"
            )
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(operations, "_git", fake_git)

    assert operations.ensure_mirror("owner/repo", "https://example.test/repo.git") == mirror
    assert attempts == 3
    assert delays == [0.1, 0.2]


def test_network_git_does_not_retry_authentication_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delays: list[float] = []
    operations = GitOperations(
        cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
        network_retry_delays=(0.1, 0.2),
        sleep=delays.append,
    )
    mirror = tmp_path / "cache" / "owner" / "repo.git"
    mirror.mkdir(parents=True)
    attempts = 0

    def fake_git(
        cwd: Path,
        *args: str,
        env: object = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(
            ["git", *args], 128, "", "fatal: Authentication failed"
        )

    monkeypatch.setattr(operations, "_git", fake_git)

    with pytest.raises(GitError, match="Authentication failed"):
        operations.ensure_mirror("owner/repo", "https://example.test/repo.git")
    assert attempts == 1
    assert delays == []


def test_transient_network_retries_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delays: list[float] = []
    operations = GitOperations(
        cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
        network_retry_delays=(0.1, 0.2),
        sleep=delays.append,
    )
    mirror = tmp_path / "cache" / "owner" / "repo.git"
    mirror.mkdir(parents=True)
    attempts = 0

    def fake_git(
        cwd: Path,
        *args: str,
        env: object = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        return subprocess.CompletedProcess(
            ["git", *args], 128, "", "fatal: unable to access: HTTP 503\n"
            "The requested URL returned error: 503"
        )

    monkeypatch.setattr(operations, "_git", fake_git)

    with pytest.raises(GitError, match="after 3 attempts") as error:
        operations.ensure_mirror("owner/repo", "https://example.test/repo.git")
    assert error.value.retryable is True
    assert attempts == 3
    assert delays == [0.1, 0.2]


def test_clone_uses_bounded_network_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = GitOperations(
        cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
    )
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def fake_network_git(
        cwd: Path, *args: str, env: object = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append((cwd, args))
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(operations, "_git_network", fake_network_git)

    mirror = operations.ensure_mirror(
        "owner/repo", "https://example.test/repo.git"
    )

    assert mirror == tmp_path / "cache" / "owner" / "repo.git"
    assert calls == [
        (
            tmp_path / "cache" / "owner",
            (
                "clone",
                "--mirror",
                "https://example.test/repo.git",
                str(mirror),
            ),
        )
    ]


def test_push_uses_bounded_network_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    operations = GitOperations(
        cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
    )
    calls: list[tuple[str, ...]] = []

    def fake_network_git(
        cwd: Path, *args: str, env: object = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(operations, "_git_network", fake_network_git)

    operations.push(
        source,
        branch="codex/12-task",
        clone_url="https://example.test/repo.git",
        token=None,
    )

    assert calls == [("push", "codex-worker-delivery", "HEAD:refs/heads/codex/12-task")]


def test_prepare_worktree_uses_exact_context_commit(tmp_path: Path) -> None:
    remote, sha = make_remote(tmp_path)
    operations = GitOperations(cache_root=tmp_path / "cache", worktree_root=tmp_path / "worktrees")

    mirror = operations.ensure_mirror("owner/repo", str(remote))
    prepared = operations.prepare_worktree(
        repo="owner/repo",
        mirror=mirror,
        context_commit=sha,
        base_branch="main",
        issue_number=12,
        slug="bounded-task",
    )

    assert prepared.branch == "codex/12-bounded-task"
    assert git(prepared.path, "rev-parse", "HEAD") == sha
    assert prepared.baseline_head == sha


def test_prepare_worktree_rejects_commit_outside_base_branch(tmp_path: Path) -> None:
    remote, sha = make_remote(tmp_path)
    source = tmp_path / "other"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    (source / "other.txt").write_text("other\n", encoding="utf-8")
    git(source, "add", "other.txt")
    git(source, "commit", "-m", "other")
    outsider = git(source, "rev-parse", "HEAD")

    operations = GitOperations(cache_root=tmp_path / "cache", worktree_root=tmp_path / "worktrees")
    mirror = operations.ensure_mirror("owner/repo", str(remote))

    with pytest.raises(GitError, match="not available"):
        operations.prepare_worktree(
            repo="owner/repo",
            mirror=mirror,
            context_commit=outsider,
            base_branch="main",
            issue_number=12,
            slug="bad",
        )


def test_diff_summary_includes_tracked_and_untracked_files(tmp_path: Path) -> None:
    remote, sha = make_remote(tmp_path)
    operations = GitOperations(cache_root=tmp_path / "cache", worktree_root=tmp_path / "worktrees")
    mirror = operations.ensure_mirror("owner/repo", str(remote))
    prepared = operations.prepare_worktree(
        repo="owner/repo",
        mirror=mirror,
        context_commit=sha,
        base_branch="main",
        issue_number=12,
        slug="diff",
    )
    (prepared.path / "README.md").write_text("baseline\nchanged\n", encoding="utf-8")
    (prepared.path / "new.txt").write_text("new\n", encoding="utf-8")

    summary = operations.diff_summary(prepared.path, sha)

    assert set(summary.changed_paths) == {"README.md", "new.txt"}
    assert summary.diff_lines == 2


def test_assert_head_unchanged_detects_agent_commit(tmp_path: Path) -> None:
    remote, sha = make_remote(tmp_path)
    operations = GitOperations(cache_root=tmp_path / "cache", worktree_root=tmp_path / "worktrees")
    mirror = operations.ensure_mirror("owner/repo", str(remote))
    prepared = operations.prepare_worktree(
        repo="owner/repo",
        mirror=mirror,
        context_commit=sha,
        base_branch="main",
        issue_number=12,
        slug="head",
    )
    git(prepared.path, "config", "user.name", "Agent")
    git(prepared.path, "config", "user.email", "agent@example.com")
    (prepared.path / "README.md").write_text("agent commit\n", encoding="utf-8")
    git(prepared.path, "add", "README.md")
    git(prepared.path, "commit", "-m", "agent")

    with pytest.raises(GitError, match="HEAD"):
        operations.assert_head_unchanged(prepared.path, sha)
