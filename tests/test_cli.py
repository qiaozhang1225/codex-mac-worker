from __future__ import annotations

import os

from codex_mac_worker.cli import build_ctl_parser, personal_github_token


def test_personal_token_prefers_environment(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token-from-env")

    assert personal_github_token() == "token-from-env"


def test_ctl_parser_supports_create_status_and_control_commands() -> None:
    parser = build_ctl_parser()

    create = parser.parse_args(
        ["task", "create", "--repo", "owner/repo", "--spec", "task.yaml", "--yes"]
    )
    status = parser.parse_args(["task", "status", "owner/repo#12"])
    pause = parser.parse_args(["task", "pause", "owner/repo#12"])

    assert create.action == "create"
    assert create.title is None
    assert create.yes is True
    assert status.action == "status"
    assert pause.action == "pause"
