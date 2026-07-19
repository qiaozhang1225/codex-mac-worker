#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from duomac_contracts import ContractError, load_project_config, parse_issue_body, validate_task
from duomac_github import GhClient, GhError, IssueRef
from duomac_git import GitSafetyError, preflight


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect dual-Mac Git delivery evidence")
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--issue", required=True)
    parser.add_argument("--project-config", required=True, type=Path)
    args = parser.parse_args()
    try:
        ref = IssueRef.parse(args.issue)
        task = parse_issue_body(GhClient().issue_body(ref))
        project = load_project_config(args.project_config)
        validate_task(task, project)
        report = preflight(args.repo_root, task, project)
    except (ContractError, GhError, GitSafetyError, OSError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "valid": True,
                "revision": task.revision,
                "repo_root": str(report.repo_root),
                "base_branch": report.base_branch,
                "start_base": report.start_base,
                "context_commit": report.context_commit,
                "changed_paths": list(report.changed_paths),
                "diff_lines": report.diff_lines,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

