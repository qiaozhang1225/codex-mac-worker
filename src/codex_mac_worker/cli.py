from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Sequence

from .assisted_merge import merge_task, review_task
from .config import load_worker_config
from .control import create_task, parse_issue_reference, send_command
from .control_state import ControlState
from .daemon import SingleInstanceLock, WorkerDaemon
from .durable_github import DurableGitHub
from .github import GitHubAppAuth, GitHubClient
from .gitops import GitOperations
from .runner import CodexRunner
from .references import IssueReference, parse_pull_request_reference
from .repository_onboarding import (
    finalize_onboarding,
    prepare_onboarding,
    repository_status,
)
from .store import EventStore
from .worker import WorkerService


def full_sha(value: str) -> str:
    if len(value) != 40 or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise argparse.ArgumentTypeError("expected a full 40-character Git SHA")
    return value.lower()


def full_hash(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise argparse.ArgumentTypeError("expected a full 64-character approval fingerprint")
    return value.lower()


def personal_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    gh = shutil.which("gh")
    if gh is None:
        raise RuntimeError("set GITHUB_TOKEN or install and authenticate GitHub CLI")
    result = subprocess.run(
        [gh, "auth", "token"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"unable to obtain GitHub token: {result.stderr.strip()}")
    return result.stdout.strip()


def codex_version(path: Path) -> str:
    result = subprocess.run(
        [str(path), "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def control_state_path() -> Path:
    configured = os.environ.get("CODEXCTL_STATE_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Library/Application Support/CodexCtl/state/codexctl.sqlite3"


def build_ctl_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codexctl")
    top = parser.add_subparsers(dest="resource", required=True)
    task = top.add_parser("task")
    actions = task.add_subparsers(dest="action", required=True)

    create = actions.add_parser("create")
    create.add_argument("--repo", required=True)
    create.add_argument("--title")
    create.add_argument("--spec", required=True, type=Path)
    create.add_argument("--yes", action="store_true")

    status = actions.add_parser("status")
    status.add_argument("reference")

    for action in ("pause", "resume", "retry", "cancel"):
        command = actions.add_parser(action)
        command.add_argument("reference")

    revise = actions.add_parser("revise")
    revise.add_argument("reference")
    revise.add_argument("--requirements", required=True, type=Path)

    review = actions.add_parser("review")
    review.add_argument("reference")

    merge = actions.add_parser("merge")
    merge.add_argument("reference")
    merge.add_argument("--expected-head", required=True, type=full_sha)
    merge.add_argument("--expected-fingerprint", required=True, type=full_hash)

    repo = top.add_parser("repo")
    repo_actions = repo.add_subparsers(dest="action", required=True)

    onboard = repo_actions.add_parser("onboard")
    onboard.add_argument("--repo", required=True)
    onboard.add_argument("--adopt-pr", type=int)
    onboard.add_argument("--project-config", type=Path)

    repo_status = repo_actions.add_parser("status")
    repo_status.add_argument("repo")

    finalize = repo_actions.add_parser("finalize")
    finalize.add_argument("reference")
    finalize.add_argument("--expected-head", required=True, type=full_sha)
    return parser


def ctl_main(argv: Sequence[str] | None = None) -> int:
    args = build_ctl_parser().parse_args(argv)
    token = personal_github_token()
    github = GitHubClient(token_provider=lambda: token)
    if args.resource == "repo":
        if args.action == "onboard":
            snapshot = prepare_onboarding(
                github,
                args.repo,
                adopt_pr=args.adopt_pr,
                project_config_path=args.project_config,
                token=token,
            )
            print(json.dumps(asdict(snapshot), ensure_ascii=False, indent=2))
            return 0
        if args.action == "status":
            report = repository_status(github, args.repo)
            print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
            return 0
        reference = parse_pull_request_reference(args.reference)
        state = ControlState(control_state_path())
        try:
            report = finalize_onboarding(
                github,
                state,
                reference,
                expected_head=args.expected_head,
            )
        finally:
            state.close()
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        return 0

    if args.action == "create":
        preview = args.spec.read_text(encoding="utf-8")
        if not args.yes:
            print(preview)
            if input("Create this GitHub task? [y/N] ").strip().lower() not in {"y", "yes"}:
                print("Cancelled")
                return 1
        result = create_task(github, args.repo, args.title, args.spec)
        print(result.get("html_url", json.dumps(result)))
        return 0

    repo, issue_number = parse_issue_reference(args.reference)
    if args.action == "review":
        snapshot = review_task(github, IssueReference(repo, issue_number))
        print(json.dumps(asdict(snapshot), ensure_ascii=False, indent=2))
        return 0 if snapshot.gates.allowed else 2
    if args.action == "merge":
        state = ControlState(control_state_path())
        try:
            result = merge_task(
                github,
                state,
                IssueReference(repo, issue_number),
                expected_head=args.expected_head,
                expected_fingerprint=args.expected_fingerprint,
            )
        finally:
            state.close()
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0
    if args.action == "status":
        issue = github.get_issue(repo, issue_number)
        summary = {
            "url": issue.get("html_url"),
            "state": issue.get("state"),
            "labels": [item.get("name") for item in issue.get("labels", [])],
            "updated_at": issue.get("updated_at"),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    requirements = args.requirements if args.action == "revise" else None
    send_command(github, repo, issue_number, args.action, requirements)
    print(f"Submitted {args.action} command for {repo}#{issue_number}")
    return 0


def worker_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-worker")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    config = load_worker_config(args.config)
    if args.check_config:
        print(
            json.dumps(
                {
                    "worker_id": config.worker_id,
                    "repositories": [item.name for item in config.repositories],
                    "discover_installation_repositories": (
                        config.discover_installation_repositories
                    ),
                    "database_path": str(config.database_path),
                    "codex_path": str(config.codex_path),
                    "codex_home": str(config.codex_home),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    lock_path = config.database_path.with_suffix(config.database_path.suffix + ".lock")
    with SingleInstanceLock(lock_path):
        auth = GitHubAppAuth(
            app_id=config.github_app_id,
            installation_id=config.github_installation_id,
            private_key_path=config.github_private_key_path,
        )
        store = EventStore(config.database_path)
        try:
            github = DurableGitHub(GitHubClient(token_provider=auth.installation_token), store)
            git = GitOperations(cache_root=config.cache_root, worktree_root=config.worktree_root)
            runner = CodexRunner(
                codex_path=config.codex_path,
                output_root=config.output_root,
                codex_home=config.codex_home,
                cli_version=codex_version(config.codex_path),
            )
            service = WorkerService(
                config=config,
                github=github,
                token_provider=auth.installation_token,
                store=store,
                git=git,
                runner=runner,
            )
            daemon = WorkerDaemon(config, github, store, service)
            if args.once:
                daemon.run_once()
            else:
                daemon.run_forever()
        finally:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(worker_main(sys.argv[1:]))
