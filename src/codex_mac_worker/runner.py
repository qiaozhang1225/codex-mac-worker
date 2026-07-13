from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable


class RunnerError(RuntimeError):
    """Raised when Codex execution fails."""


class RunnerTimeout(RunnerError):
    """Raised when a bounded Codex attempt exceeds its wall-clock limit."""


@dataclass(frozen=True, slots=True)
class RunnerResult:
    exit_code: int
    session_id: str | None
    events: tuple[dict[str, Any], ...]
    last_message: str
    stderr: str
    termination_reason: str | None = None
    model: str | None = None
    cli_version: str | None = None


_SECRET_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "API_KEY",
    "PRIVATE_KEY",
    "AWS_",
    "ALIYUN_",
    "DEPLOY_",
    "GITHUB_",
    "GH_",
)


def scrubbed_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in _SECRET_MARKERS)
    }


class CodexRunner:
    def __init__(
        self,
        *,
        codex_path: Path,
        output_root: Path,
        codex_home: Path | None = None,
        cli_version: str | None = None,
    ) -> None:
        self.codex_path = codex_path
        self.output_root = output_root
        self.codex_home = codex_home
        self.cli_version = cli_version
        self._current_process: subprocess.Popen[str] | None = None
        self._stop_requested: str | None = None

    def stop_current(self) -> None:
        process = self._current_process
        if process is None or process.poll() is not None:
            return
        self._stop_requested = "pause"
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    def run(
        self,
        worktree: Path,
        prompt: str,
        output_schema: Path,
        *,
        timeout_seconds: float,
        heartbeat_callback: Callable[[], None] | None = None,
        heartbeat_interval_seconds: float = 120,
        control_callback: Callable[[], str | None] | None = None,
        resume_session_id: str | None = None,
    ) -> RunnerResult:
        self.output_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.output_root) as temporary:
            last_message_path = Path(temporary) / "last-message.txt"
            if resume_session_id:
                command = [
                    str(self.codex_path),
                    "exec",
                    "resume",
                    "--strict-config",
                    "--json",
                    "--output-schema",
                    str(output_schema),
                    "--output-last-message",
                    str(last_message_path),
                    resume_session_id,
                    "-",
                ]
            else:
                command = [
                    str(self.codex_path),
                    "exec",
                    "--strict-config",
                    "--json",
                    "--output-schema",
                    str(output_schema),
                    "--output-last-message",
                    str(last_message_path),
                    "--cd",
                    str(worktree),
                    "-",
                ]
            environment = scrubbed_environment()
            if self.codex_home is not None:
                environment["CODEX_HOME"] = str(self.codex_home)
            process = subprocess.Popen(
                command,
                cwd=worktree if resume_session_id else None,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                start_new_session=True,
            )
            self._current_process = process
            self._stop_requested = None
            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None
            process.stdin.write(prompt)
            process.stdin.close()

            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            def drain(stream: Any, target: list[str]) -> None:
                target.extend(iter(stream.readline, ""))
                stream.close()

            stdout_thread = threading.Thread(
                target=drain,
                args=(process.stdout, stdout_lines),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=drain,
                args=(process.stderr, stderr_lines),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            def stop_process() -> None:
                if process.poll() is not None:
                    return
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)

            started = time.monotonic()
            next_heartbeat = started + heartbeat_interval_seconds
            termination_reason: str | None = None
            while process.poll() is None:
                now = time.monotonic()
                if now - started >= timeout_seconds:
                    stop_process()
                    self._current_process = None
                    stdout_thread.join(timeout=1)
                    stderr_thread.join(timeout=1)
                    raise RunnerTimeout(f"Codex attempt exceeded {timeout_seconds} seconds")
                if control_callback is not None:
                    requested = control_callback()
                    if requested in {"pause", "cancel"}:
                        termination_reason = requested
                        stop_process()
                        break
                if heartbeat_callback is not None and now >= next_heartbeat:
                    heartbeat_callback()
                    next_heartbeat = now + heartbeat_interval_seconds
                time.sleep(min(0.1, max(0.01, timeout_seconds / 20)))
            process.wait()
            self._current_process = None
            termination_reason = termination_reason or self._stop_requested
            self._stop_requested = None
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            stdout = "".join(stdout_lines)
            stderr = "".join(stderr_lines)

            events: list[dict[str, Any]] = []
            session_id: str | None = None
            model: str | None = None
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                events.append(event)
                candidate = event.get("thread_id") or event.get("session_id")
                if isinstance(candidate, str) and candidate:
                    session_id = candidate
                model_candidate = event.get("model") or event.get("model_name")
                if isinstance(model_candidate, str) and model_candidate:
                    model = model_candidate
            last_message = ""
            if last_message_path.exists():
                last_message = last_message_path.read_text(encoding="utf-8")
            return RunnerResult(
                exit_code=process.returncode,
                session_id=session_id,
                events=tuple(events),
                last_message=last_message,
                stderr=stderr,
                termination_reason=termination_reason,
                model=model,
                cli_version=self.cli_version,
            )
