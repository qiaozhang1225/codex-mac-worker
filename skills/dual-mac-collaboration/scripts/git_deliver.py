#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from duomac_contracts import ContractError, load_project_config, parse_issue_body, validate_task
from duomac_github import GhClient, GhError, IssueRef
from duomac_git import GitSafetyError, deliver, preflight


def _verification_runner(repo_root: Path):
    def run(commands: tuple[str, ...]) -> None:
        for command in commands:
            env = os.environ.copy()
            env["GIT_TERMINAL_PROMPT"] = "0"
            result = subprocess.run(
                ["/bin/zsh", "-lc", command],
                cwd=repo_root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or "no output"
                raise GitSafetyError(f"verification failed ({command}): {detail}")

    return run


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview or perform guarded Git delivery")
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--project-config", required=True, type=Path)
    parser.add_argument("--start-base", required=True)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    try:
        ref = IssueRef.parse(args.issue)
        task = parse_issue_body(GhClient().issue_body(ref))
        project = load_project_config(args.project_config)
        validate_task(task, project)
        report = preflight(args.repo_root, task, project)
        if report.start_base != args.start_base:
            raise GitSafetyError("start-base does not match current task evidence")
        target = (
            f"refs/heads/{project.default_base_branch}"
            if task.delivery_mode == "direct-main"
            else "refs/heads/" + subprocess.run(
                ["git", "-C", str(args.repo_root), "branch", "--show-current"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
        )
        if not args.yes:
            print(
                json.dumps(
                    {
                        "applied": False,
                        "revision": task.revision,
                        "delivery_mode": task.delivery_mode,
                        "target": target,
                        "changed_paths": list(report.changed_paths),
                        "verification": list(project.verification[task.verification_profile]),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        delivery = deliver(
            args.repo_root,
            task=task,
            project=project,
            start_base=args.start_base,
            run_verification=_verification_runner(args.repo_root),
        )
    except (
        ContractError,
        GhError,
        GitSafetyError,
        OSError,
        KeyError,
        subprocess.SubprocessError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "applied": True,
                "state": delivery.state,
                "commit": delivery.commit_sha,
                "branch": delivery.branch,
                "rebased": delivery.rebased,
                "verification_runs": delivery.verification_runs,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

