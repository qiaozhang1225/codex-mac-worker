from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Callable, Literal

from duomac_contracts import ProjectConfig, TaskSpec


_FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


class GitSafetyError(RuntimeError):
    """Raised when repository evidence does not permit safe delivery."""


@dataclass(frozen=True, slots=True)
class PreflightReport:
    repo_root: Path
    base_branch: str
    start_base: str
    context_commit: str
    context_is_ancestor: bool
    changed_paths: tuple[str, ...]
    diff_lines: int


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    state: Literal["delivered", "completed"]
    commit_sha: str
    branch: str
    rebased: bool
    verification_runs: int


def _git(
    repo_root: Path,
    *args: str,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise GitSafetyError(f"git {' '.join(args)} failed: {detail}")
    return result


def _output(repo_root: Path, *args: str) -> str:
    return _git(repo_root, *args).stdout.strip()


def _is_ancestor(repo_root: Path, older: str, newer: str) -> bool:
    result = _git(repo_root, "merge-base", "--is-ancestor", older, newer, check=False)
    if result.returncode not in {0, 1}:
        detail = result.stderr.strip() or "unable to compare commits"
        raise GitSafetyError(detail)
    return result.returncode == 0


def _changed_paths(repo_root: Path, older: str, newer: str) -> tuple[str, ...]:
    output = _output(repo_root, "diff", "--name-only", "--diff-filter=ACDMRTUXB", f"{older}..{newer}")
    return tuple(sorted(line for line in output.splitlines() if line))


def _path_within(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _paths_overlap(left: str, right: str) -> bool:
    return _path_within(left, right) or _path_within(right, left)


def _assert_repository(repo_root: Path) -> Path:
    root = repo_root.expanduser().resolve()
    if not root.is_dir():
        raise GitSafetyError(f"repository root does not exist: {root}")
    top = Path(_output(root, "rev-parse", "--show-toplevel")).resolve()
    if top != root:
        raise GitSafetyError(f"repo-root must be the Git top level: {top}")
    return root


def _assert_clean_task_branch(repo_root: Path, base_branch: str) -> str:
    if _output(repo_root, "status", "--porcelain", "--untracked-files=all"):
        raise GitSafetyError("task worktree must be clean before delivery")
    branch_result = _git(
        repo_root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
    )
    if branch_result.returncode != 0:
        raise GitSafetyError("detached HEAD is not allowed")
    branch = branch_result.stdout.strip()
    if branch == base_branch or not branch.startswith("codex/"):
        raise GitSafetyError("delivery must run from a codex/* task branch")
    return branch


def _diff_evidence(repo_root: Path, older: str, newer: str) -> tuple[int, bool]:
    numstat = _output(repo_root, "diff", "--numstat", f"{older}..{newer}")
    lines = 0
    binary = False
    for row in numstat.splitlines():
        if not row:
            continue
        parts = row.split("\t", 2)
        if len(parts) != 3:
            raise GitSafetyError("unexpected Git numstat output")
        if parts[0] == "-" or parts[1] == "-":
            binary = True
            continue
        lines += int(parts[0]) + int(parts[1])
    return lines, binary


def _has_submodule_change(repo_root: Path, older: str, newer: str) -> bool:
    raw = _output(repo_root, "diff", "--raw", f"{older}..{newer}")
    return any(
        line.startswith(":160000 ") or " 160000 " in line.split("\t", 1)[0]
        for line in raw.splitlines()
    )


def preflight(
    repo_root: Path, task: TaskSpec, project: ProjectConfig
) -> PreflightReport:
    root = _assert_repository(repo_root)
    _assert_clean_task_branch(root, project.default_base_branch)
    head = _output(root, "rev-parse", "HEAD")
    base_ref = f"refs/remotes/origin/{project.default_base_branch}"
    start_base = _output(root, "merge-base", head, base_ref)
    if not _is_ancestor(root, start_base, head):
        raise GitSafetyError("task branch is not based on the default branch")
    context_exists = _git(
        root, "cat-file", "-e", f"{task.context_commit}^{{commit}}", check=False
    )
    if context_exists.returncode != 0:
        raise GitSafetyError("context commit does not exist in this repository")
    context_is_ancestor = _is_ancestor(root, task.context_commit, start_base)
    changed_paths = _changed_paths(root, start_base, head)
    diff_lines, has_binary = _diff_evidence(root, start_base, head)
    report = PreflightReport(
        repo_root=root,
        base_branch=project.default_base_branch,
        start_base=start_base,
        context_commit=task.context_commit,
        context_is_ancestor=context_is_ancestor,
        changed_paths=changed_paths,
        diff_lines=diff_lines,
    )
    validate_scope(report, task, project)
    if has_binary:
        raise GitSafetyError("binary changes are not allowed")
    if _has_submodule_change(root, start_base, head):
        raise GitSafetyError("submodule changes are not allowed")
    if any(PurePosixPath(path).name.startswith(".env") for path in changed_paths):
        raise GitSafetyError("tracked .env files are not allowed")
    return report


def validate_scope(
    report: PreflightReport, task: TaskSpec, project: ProjectConfig
) -> None:
    if not report.context_is_ancestor:
        raise GitSafetyError("context commit is not an ancestor of the task base")
    if not report.changed_paths:
        raise GitSafetyError("task branch has no committed changes")
    if len(report.changed_paths) > project.max_changed_files:
        raise GitSafetyError("changed file count exceeds the project limit")
    if report.diff_lines > project.max_diff_lines:
        raise GitSafetyError("diff line count exceeds the project limit")
    for path in report.changed_paths:
        if not any(_path_within(path, allowed) for allowed in task.allowed_paths):
            raise GitSafetyError(f"changed path is outside allowed paths: {path}")
        if any(_paths_overlap(path, protected) for protected in project.protected_paths):
            raise GitSafetyError(f"changed path overlaps a protected path: {path}")


def _fetch_base(repo_root: Path, branch: str) -> str:
    _git(
        repo_root,
        "fetch",
        "--no-tags",
        "origin",
        f"refs/heads/{branch}:refs/remotes/origin/{branch}",
    )
    return _output(repo_root, "rev-parse", f"refs/remotes/origin/{branch}")


def _assert_start_base(repo_root: Path, start_base: str, head: str) -> None:
    if _FULL_SHA.fullmatch(start_base) is None:
        raise GitSafetyError("start-base must be a full commit SHA")
    if not _is_ancestor(repo_root, start_base, head):
        raise GitSafetyError("start-base is not an ancestor of the task HEAD")


def _assert_no_drift_overlap(
    repo_root: Path, start_base: str, remote_tip: str, task_paths: tuple[str, ...]
) -> None:
    if not _is_ancestor(repo_root, start_base, remote_tip):
        raise GitSafetyError("remote default branch diverged from start-base")
    remote_paths = _changed_paths(repo_root, start_base, remote_tip)
    overlaps = sorted(
        {remote for remote in remote_paths for task in task_paths if _paths_overlap(remote, task)}
    )
    if overlaps:
        raise GitSafetyError(
            "remote changes overlap task paths: " + ", ".join(overlaps)
        )


def _push_result(repo_root: Path, target: str) -> subprocess.CompletedProcess[str]:
    return _git(repo_root, "push", "origin", f"HEAD:{target}", check=False)


def _push_error(result: subprocess.CompletedProcess[str], message: str) -> GitSafetyError:
    detail = result.stderr.strip() or result.stdout.strip() or "unknown push error"
    return GitSafetyError(f"{message}: {detail}")


def _rebase_and_verify(
    repo_root: Path,
    remote_tip: str,
    task: TaskSpec,
    project: ProjectConfig,
    commands: tuple[str, ...],
    run_verification: Callable[[tuple[str, ...]], None],
) -> None:
    result = _git(repo_root, "rebase", remote_tip, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "rebase conflict"
        raise GitSafetyError(f"rebase blocked by conflict: {detail}")
    preflight(repo_root, task, project)
    run_verification(commands)
    preflight(repo_root, task, project)


def deliver(
    repo_root: Path,
    *,
    task: TaskSpec,
    project: ProjectConfig,
    start_base: str,
    run_verification: Callable[[tuple[str, ...]], None],
) -> DeliveryReport:
    root = _assert_repository(repo_root)
    branch = _assert_clean_task_branch(root, project.default_base_branch)
    head = _output(root, "rev-parse", "HEAD")
    _assert_start_base(root, start_base, head)
    initial = preflight(root, task, project)
    if initial.start_base != start_base:
        raise GitSafetyError("start-base does not match the task branch base")
    commands = project.verification[task.verification_profile]
    verification_runs = 0
    run_verification(commands)
    verification_runs += 1
    preflight(root, task, project)

    if task.delivery_mode == "task-branch":
        push = _push_result(root, f"refs/heads/{branch}")
        if push.returncode != 0:
            raise _push_error(push, "task-branch push rejected; do not retry automatically")
        return DeliveryReport(
            state="delivered",
            commit_sha=_output(root, "rev-parse", "HEAD"),
            branch=branch,
            rebased=False,
            verification_runs=verification_runs,
        )

    remote_tip = _fetch_base(root, project.default_base_branch)
    rebased = False
    if remote_tip != start_base:
        _assert_no_drift_overlap(root, start_base, remote_tip, initial.changed_paths)
        _rebase_and_verify(
            root, remote_tip, task, project, commands, run_verification
        )
        rebased = True
        verification_runs += 1

    target = f"refs/heads/{project.default_base_branch}"
    push = _push_result(root, target)
    if push.returncode != 0 and not rebased:
        refreshed_tip = _fetch_base(root, project.default_base_branch)
        if refreshed_tip == remote_tip:
            raise _push_error(push, "push rejected without remote drift")
        _assert_no_drift_overlap(
            root, start_base, refreshed_tip, initial.changed_paths
        )
        _rebase_and_verify(
            root, refreshed_tip, task, project, commands, run_verification
        )
        rebased = True
        verification_runs += 1
        push = _push_result(root, target)
    if push.returncode != 0:
        raise _push_error(push, "push rejected after the single allowed drift refresh")
    return DeliveryReport(
        state="completed",
        commit_sha=_output(root, "rev-parse", "HEAD"),
        branch=branch,
        rebased=rebased,
        verification_runs=verification_runs,
    )
