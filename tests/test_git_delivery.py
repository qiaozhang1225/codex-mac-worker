from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from duomac_contracts import ProjectConfig, TaskSpec, render_issue_body
from duomac_git import GitSafetyError, deliver, preflight, validate_scope


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "dual-mac-collaboration" / "scripts"


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def configure(path: Path) -> None:
    git(path, "config", "user.name", "Dual Mac Test")
    git(path, "config", "user.email", "duomac@example.invalid")


@dataclass
class RepoFixture:
    remote: Path
    task_worktree: Path
    task: TaskSpec
    project: ProjectConfig
    start_base: str
    verification_runs: int = 0

    def pass_verification(self, commands: tuple[str, ...]) -> None:
        assert commands == ("test -f product/frontend/src/history/card.tsx",)
        self.verification_runs += 1

    def remote_ref(self, ref: str) -> str:
        return git(self.remote, "rev-parse", ref)

    def advance_remote(self, path: str) -> str:
        peer = self.remote.parent / f"peer-{self.verification_runs}-{Path(path).name}"
        subprocess.run(
            ["git", "clone", str(self.remote), str(peer)],
            text=True,
            capture_output=True,
            check=True,
        )
        configure(peer)
        target = peer / path
        target.parent.mkdir(parents=True, exist_ok=True)
        previous = target.read_text(encoding="utf-8") if target.exists() else ""
        target.write_text(previous + "remote change\n", encoding="utf-8")
        git(peer, "add", path)
        git(peer, "commit", "-m", "peer change")
        git(peer, "push", "origin", "main")
        return git(peer, "rev-parse", "HEAD")


@pytest.fixture
def repo_fixture(tmp_path: Path) -> RepoFixture:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
    configure(seed)
    card = seed / "product/frontend/src/history/card.tsx"
    card.parent.mkdir(parents=True)
    card.write_text("export const width = 'old';\n", encoding="utf-8")
    (seed / "docs").mkdir()
    (seed / "docs/product.md").write_text("approved\n", encoding="utf-8")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "initial")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-u", "origin", "main")
    subprocess.run(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )
    start_base = git(seed, "rev-parse", "HEAD")

    task_worktree = tmp_path / "task"
    subprocess.run(
        ["git", "clone", str(remote), str(task_worktree)],
        check=True,
        capture_output=True,
    )
    configure(task_worktree)
    git(task_worktree, "switch", "-c", "codex/7-layout")
    task_card = task_worktree / "product/frontend/src/history/card.tsx"
    task_card.write_text("export const width = 'full';\n", encoding="utf-8")
    git(task_worktree, "add", ".")
    git(task_worktree, "commit", "-m", "fix history card")

    task = TaskSpec(
        revision=1,
        dispatcher="macbook",
        executor="mac-mini",
        objective="Fix the history card",
        context_commit=start_base,
        context_files=("docs/product.md",),
        decisions=("Keep backend unchanged",),
        acceptance=("The card uses full width",),
        allowed_paths=("product/frontend/src/history",),
        out_of_scope=("Backend",),
        execution_plan=("Update card", "Verify"),
        verification_profile="fast",
        delivery_mode="direct-main",
        risk="low",
    )
    project = ProjectConfig(
        default_base_branch="main",
        protected_paths=(".github/workflows", ".env", "product/deploy"),
        max_changed_files=5,
        max_diff_lines=100,
        verification={
            "fast": ("test -f product/frontend/src/history/card.tsx",),
        },
    )
    return RepoFixture(remote, task_worktree, task, project, start_base)


def test_preflight_reports_committed_scope(repo_fixture: RepoFixture) -> None:
    report = preflight(repo_fixture.task_worktree, repo_fixture.task, repo_fixture.project)

    validate_scope(report, repo_fixture.task, repo_fixture.project)
    assert report.start_base == repo_fixture.start_base
    assert report.context_is_ancestor is True
    assert report.changed_paths == ("product/frontend/src/history/card.tsx",)
    assert report.diff_lines == 2


def test_direct_main_pushes_verified_head_when_remote_is_unchanged(
    repo_fixture: RepoFixture,
) -> None:
    report = deliver(
        repo_fixture.task_worktree,
        task=repo_fixture.task,
        project=repo_fixture.project,
        start_base=repo_fixture.start_base,
        run_verification=repo_fixture.pass_verification,
    )

    assert report.state == "completed"
    assert report.rebased is False
    assert report.verification_runs == 1
    assert repo_fixture.remote_ref("refs/heads/main") == report.commit_sha


def test_direct_main_rebases_once_when_non_overlapping(
    repo_fixture: RepoFixture,
) -> None:
    remote_tip = repo_fixture.advance_remote("docs/unrelated.md")

    report = deliver(
        repo_fixture.task_worktree,
        task=repo_fixture.task,
        project=repo_fixture.project,
        start_base=repo_fixture.start_base,
        run_verification=repo_fixture.pass_verification,
    )

    assert report.rebased is True
    assert report.verification_runs == 2
    assert git(repo_fixture.task_worktree, "merge-base", "HEAD", remote_tip) == remote_tip
    assert repo_fixture.remote_ref("refs/heads/main") == report.commit_sha


def test_direct_main_blocks_on_overlapping_remote_change(
    repo_fixture: RepoFixture,
) -> None:
    remote_tip = repo_fixture.advance_remote("product/frontend/src/history/card.tsx")

    with pytest.raises(GitSafetyError, match="overlap"):
        deliver(
            repo_fixture.task_worktree,
            task=repo_fixture.task,
            project=repo_fixture.project,
            start_base=repo_fixture.start_base,
            run_verification=repo_fixture.pass_verification,
        )

    assert repo_fixture.remote_ref("refs/heads/main") == remote_tip


def test_task_branch_pushes_only_task_branch(repo_fixture: RepoFixture) -> None:
    task = TaskSpec(
        **{
            field: getattr(repo_fixture.task, field)
            for field in repo_fixture.task.__dataclass_fields__
            if field != "delivery_mode"
        },
        delivery_mode="task-branch",
    )
    original_main = repo_fixture.remote_ref("refs/heads/main")

    report = deliver(
        repo_fixture.task_worktree,
        task=task,
        project=repo_fixture.project,
        start_base=repo_fixture.start_base,
        run_verification=repo_fixture.pass_verification,
    )

    assert report.state == "delivered"
    assert repo_fixture.remote_ref("refs/heads/main") == original_main
    assert repo_fixture.remote_ref("refs/heads/codex/7-layout") == report.commit_sha


def test_rejects_dirty_worktree(repo_fixture: RepoFixture) -> None:
    card = repo_fixture.task_worktree / "product/frontend/src/history/card.tsx"
    card.write_text(card.read_text(encoding="utf-8") + "dirty\n", encoding="utf-8")

    with pytest.raises(GitSafetyError, match="clean"):
        deliver(
            repo_fixture.task_worktree,
            task=repo_fixture.task,
            project=repo_fixture.project,
            start_base=repo_fixture.start_base,
            run_verification=repo_fixture.pass_verification,
        )


def test_rejects_out_of_scope_commit(repo_fixture: RepoFixture) -> None:
    path = repo_fixture.task_worktree / "backend/api.py"
    path.parent.mkdir()
    path.write_text("unsafe = True\n", encoding="utf-8")
    git(repo_fixture.task_worktree, "add", ".")
    git(repo_fixture.task_worktree, "commit", "-m", "widen scope")

    with pytest.raises(GitSafetyError, match="outside allowed paths"):
        preflight(repo_fixture.task_worktree, repo_fixture.task, repo_fixture.project)


def test_rejects_detached_head(repo_fixture: RepoFixture) -> None:
    git(repo_fixture.task_worktree, "checkout", "--detach")

    with pytest.raises(GitSafetyError, match="detached"):
        preflight(repo_fixture.task_worktree, repo_fixture.task, repo_fixture.project)


def test_source_contains_no_force_push_flags() -> None:
    source = (SCRIPTS / "duomac_git.py").read_text(encoding="utf-8")

    assert "--force" not in source
    assert "--force-with-lease" not in source
    assert "+refs" not in source


def test_git_deliver_cli_is_dry_run_without_yes(
    repo_fixture: RepoFixture, tmp_path: Path
) -> None:
    project_path = tmp_path / "project.toml"
    project_path.write_text(
        '''schema_version = 1
default_base_branch = "main"
protected_paths = [".github/workflows", ".env", "product/deploy"]
max_changed_files = 5
max_diff_lines = 100

[verification.fast]
commands = ["test -f product/frontend/src/history/card.tsx"]
''',
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh_log = tmp_path / "gh.log"
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
Path(os.environ["GH_LOG"]).write_text(json.dumps(sys.argv[1:]))
print(json.dumps({"body": os.environ["ISSUE_BODY"]}))
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GH_LOG": str(gh_log),
        "ISSUE_BODY": render_issue_body(repo_fixture.task),
    }

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "git_deliver.py"),
            "--repo-root",
            str(repo_fixture.task_worktree),
            "--issue",
            "https://github.com/owner/repo/issues/7",
            "--project-config",
            str(project_path),
            "--start-base",
            repo_fixture.start_base,
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["applied"] is False
    assert output["target"] == "refs/heads/main"
    assert output["verification"] == ["test -f product/frontend/src/history/card.tsx"]
    assert repo_fixture.remote_ref("refs/heads/main") == repo_fixture.start_base
