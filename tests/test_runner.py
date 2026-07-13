from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import time

import pytest

from codex_mac_worker.runner import CodexRunner, RunnerTimeout


def make_fake_codex(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-codex"
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_runner_uses_bounded_exec_flags_and_scrubs_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    capture = tmp_path / "capture.json"
    fake = make_fake_codex(
        tmp_path,
        """
import json, os, pathlib, sys
capture = pathlib.Path(os.environ["CAPTURE_PATH"])
capture.write_text(json.dumps({
    "args": sys.argv[1:],
    "prompt": sys.stdin.read(),
    "github_token": os.environ.get("GITHUB_TOKEN"),
    "deploy_secret": os.environ.get("DEPLOY_SECRET"),
    "codex_home": os.environ.get("CODEX_HOME"),
}), encoding="utf-8")
print(json.dumps({"type": "thread.started", "thread_id": "session-123"}))
print(json.dumps({"type": "item.completed"}))
""",
    )
    schema = tmp_path / "result.schema.json"
    schema.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CAPTURE_PATH", str(capture))
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    monkeypatch.setenv("DEPLOY_SECRET", "must-not-leak")
    codex_home = tmp_path / "codex-home"
    runner = CodexRunner(
        codex_path=fake,
        output_root=tmp_path / "outputs",
        codex_home=codex_home,
    )

    result = runner.run(tmp_path, "bounded prompt", schema, timeout_seconds=5)
    data = json.loads(capture.read_text())

    assert result.session_id == "session-123"
    assert data["prompt"] == "bounded prompt"
    assert data["github_token"] is None
    assert data["deploy_secret"] is None
    assert data["codex_home"] == str(codex_home)
    assert "exec" in data["args"]
    assert "--strict-config" in data["args"]
    assert "--sandbox" not in data["args"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in data["args"]
    assert "/goal" not in data["prompt"].lower()


def test_runner_kills_timed_out_process_group(tmp_path: Path) -> None:
    fake = make_fake_codex(tmp_path, "import time\ntime.sleep(30)\n")
    schema = tmp_path / "result.schema.json"
    schema.write_text("{}", encoding="utf-8")
    runner = CodexRunner(codex_path=fake, output_root=tmp_path / "outputs")

    started = time.monotonic()
    with pytest.raises(RunnerTimeout):
        runner.run(tmp_path, "bounded prompt", schema, timeout_seconds=0.1)

    assert time.monotonic() - started < 5


def test_runner_emits_heartbeats_while_process_is_active(tmp_path: Path) -> None:
    fake = make_fake_codex(tmp_path, "import time\ntime.sleep(0.35)\n")
    schema = tmp_path / "result.schema.json"
    schema.write_text("{}", encoding="utf-8")
    runner = CodexRunner(codex_path=fake, output_root=tmp_path / "outputs")
    heartbeats: list[float] = []

    runner.run(
        tmp_path,
        "bounded prompt",
        schema,
        timeout_seconds=5,
        heartbeat_callback=lambda: heartbeats.append(time.monotonic()),
        heartbeat_interval_seconds=0.05,
    )

    assert len(heartbeats) >= 2


def test_runner_stops_on_explicit_control_command(tmp_path: Path) -> None:
    fake = make_fake_codex(tmp_path, "import time\ntime.sleep(30)\n")
    schema = tmp_path / "result.schema.json"
    schema.write_text("{}", encoding="utf-8")
    runner = CodexRunner(codex_path=fake, output_root=tmp_path / "outputs")

    result = runner.run(
        tmp_path,
        "bounded prompt",
        schema,
        timeout_seconds=5,
        control_callback=lambda: "cancel",
    )

    assert result.termination_reason == "cancel"
    assert result.exit_code != 0


def test_runner_resumes_only_the_explicit_session_in_same_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "resume.json"
    fake = make_fake_codex(
        tmp_path,
        """
import json, os, pathlib, sys
pathlib.Path(os.environ["CAPTURE_PATH"]).write_text(json.dumps({
    "args": sys.argv[1:], "cwd": os.getcwd(), "prompt": sys.stdin.read()
}), encoding="utf-8")
""",
    )
    schema = tmp_path / "result.schema.json"
    schema.write_text("{}", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("CAPTURE_PATH", str(capture))
    runner = CodexRunner(codex_path=fake, output_root=tmp_path / "outputs")

    runner.run(
        worktree,
        "continue bounded work",
        schema,
        timeout_seconds=5,
        resume_session_id="11111111-1111-1111-1111-111111111111",
    )
    data = json.loads(capture.read_text(encoding="utf-8"))

    assert data["args"][:2] == ["exec", "resume"]
    assert "11111111-1111-1111-1111-111111111111" in data["args"]
    assert not any("sandbox_mode" in item for item in data["args"])
    assert data["cwd"] == str(worktree)
