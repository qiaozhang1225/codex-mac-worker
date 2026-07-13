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

