from __future__ import annotations

from pathlib import Path
import json
import stat
import sys

import pytest

from codex_mac_worker.config import load_project_config
from codex_mac_worker.prompting import build_execution_prompt, build_revision_prompt, result_schema
from codex_mac_worker.protocol import parse_task_body
from codex_mac_worker.verification import (
    VerificationError,
    run_commands,
    scan_for_secrets,
    run_verification,
)

from .test_config_policy import write_config
from .test_protocol import task_body


def test_execution_prompt_is_bounded_and_never_mentions_goal_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "project.toml"
    write_config(config_path)
    spec = parse_task_body(task_body())

    prompt = build_execution_prompt(spec, issue_number=12)

    assert spec.objective in prompt
    assert "Do not commit, push, open a pull request, deploy, or merge" in prompt
    assert "Only modify these paths" in prompt
    assert "/goal" not in prompt.lower()
    assert "goal mode" not in prompt.lower()
    assert result_schema()["required"] == [
        "status",
        "summary",
        "changed_files",
        "risks",
        "needs_human",
        "acceptance_results",
    ]
    acceptance = result_schema()["properties"]["acceptance_results"]
    assert acceptance["items"]["properties"]["status"]["enum"] == [
        "met",
        "not_met",
        "needs_review",
    ]


def test_revision_prompt_starts_new_bounded_attempt() -> None:
    spec = parse_task_body(task_body())

    prompt = build_revision_prompt(spec, "Add the missing empty-state test", "diff summary")

    assert "Add the missing empty-state test" in prompt
    assert "diff summary" in prompt
    assert "new bounded attempt" in prompt


def test_run_verification_uses_only_configured_commands(tmp_path: Path) -> None:
    config_path = tmp_path / "project.toml"
    config_path.write_text(
        f"""
schema_version = 2
default_base_branch = "main"
worker_github_app_id = 777
allowed_risk_levels = ["low"]
protected_paths = [".codex-worker"]
max_changed_files = 3
max_diff_lines = 20
codex_attempt_timeout_minutes = 45
task_hard_timeout_minutes = 120
max_automatic_attempts = 2
[verification.fast]
commands = ["{sys.executable} -c 'print(123)'", "{sys.executable} -c 'import sys; sys.exit(2)'"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_project_config(config_path)

    result = run_verification(tmp_path, config, "fast", timeout_seconds=5)

    assert result.passed is False
    assert len(result.commands) == 2
    assert result.commands[0].exit_code == 0
    assert result.commands[1].exit_code == 2


def test_verification_uses_worker_permission_profile_and_scrubbed_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "sandbox.json"
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps({{"
        "'args': args, 'codex_home': os.environ.get('CODEX_HOME'), "
        "'github_token': os.environ.get('GITHUB_TOKEN')}))\n"
        "index = args.index('--')\n"
        "raise SystemExit(subprocess.run(args[index + 1:]).returncode)\n",
        encoding="utf-8",
    )
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IEXEC)
    config_path = tmp_path / "project.toml"
    write_config(config_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "python -m unittest", f"{sys.executable} -c 'print(123)'"
        ),
        encoding="utf-8",
    )
    config = load_project_config(config_path)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")

    result = run_verification(
        tmp_path,
        config,
        "fast",
        timeout_seconds=5,
        codex_path=fake_codex,
        codex_home=codex_home,
    )
    data = json.loads(capture.read_text(encoding="utf-8"))

    assert result.passed is True
    assert data["args"][:4] == ["sandbox", "-P", "codex-worker", "-C"]
    assert data["codex_home"] == str(codex_home)
    assert data["github_token"] is None


def test_command_runner_stops_process_group_on_cancel(tmp_path: Path) -> None:
    result = run_commands(
        tmp_path,
        (f"{sys.executable} -c 'import time; time.sleep(30)'",),
        timeout_seconds=5,
        control_callback=lambda: "cancel",
    )

    assert result.passed is False
    assert result.termination_reason == "cancel"


def test_secret_scanner_rejects_private_keys_and_tokens(tmp_path: Path) -> None:
    safe = tmp_path / "safe.txt"
    unsafe = tmp_path / "unsafe.txt"
    safe.write_text("ordinary text\n", encoding="utf-8")
    unsafe.write_text("-----BEGIN PRIVATE KEY-----\nsecret\n", encoding="utf-8")

    with pytest.raises(VerificationError, match="secret-like"):
        scan_for_secrets(tmp_path, ["safe.txt", "unsafe.txt"])


@pytest.mark.parametrize(
    "secret",
    [
        'EASEWISE_OSS_ACCESS_KEY_SECRET="abcdefghijklmnop"',
        'password = "this-is-a-real-password"',
        "LTAI5tExampleAccessKey1234",
    ],
)
def test_secret_scanner_rejects_easewise_and_generic_credentials(
    tmp_path: Path, secret: str
) -> None:
    unsafe = tmp_path / "unsafe.txt"
    unsafe.write_text(secret, encoding="utf-8")

    with pytest.raises(VerificationError, match="secret-like"):
        scan_for_secrets(tmp_path, ["unsafe.txt"])


def test_secret_scanner_skips_binary_but_rejects_large_binary(tmp_path: Path) -> None:
    binary = tmp_path / "asset.bin"
    binary.write_bytes(b"\x00" * 1025)

    with pytest.raises(VerificationError, match="binary"):
        scan_for_secrets(tmp_path, ["asset.bin"], max_binary_bytes=1024)
