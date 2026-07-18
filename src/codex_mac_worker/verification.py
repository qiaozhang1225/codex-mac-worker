from __future__ import annotations

from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
from typing import Callable

from .config import ProjectConfig


class VerificationError(RuntimeError):
    """Raised when generated changes fail an integrity or secret check."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: str
    exit_code: int
    output: str


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    commands: tuple[CommandResult, ...]
    termination_reason: str | None = None


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"LTAI[A-Za-z0-9]{16,}"),
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?(?:key[_-]?secret|token)|client[_-]?secret|password)"
        r"['\"]?\s*[:=]\s*['\"][^'\"]{12,}"
    ),
)


def _secret_matches(text: str) -> Counter[str]:
    return Counter(
        match.group(0)
        for pattern in _SECRET_PATTERNS
        for match in pattern.finditer(text)
    )


def _baseline_text(worktree: Path, baseline_ref: str, relative: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{baseline_ref}:{relative}"],
        cwd=worktree,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", errors="replace")


def scan_for_secrets(
    worktree: Path,
    changed_paths: list[str] | tuple[str, ...],
    *,
    baseline_ref: str | None = None,
    max_binary_bytes: int = 1_000_000,
) -> None:
    if baseline_ref is not None:
        baseline = subprocess.run(
            ["git", "cat-file", "-e", f"{baseline_ref}^{{commit}}"],
            cwd=worktree,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if baseline.returncode != 0:
            raise VerificationError("secret scan baseline commit is unavailable")
    for relative in changed_paths:
        path = worktree / relative
        if not path.exists() or path.is_dir():
            continue
        data = path.read_bytes()
        if b"\x00" in data:
            if len(data) > max_binary_bytes:
                raise VerificationError(f"binary file exceeds limit: {relative}")
            continue
        text = data.decode("utf-8", errors="replace")
        matches = _secret_matches(text)
        if baseline_ref is not None:
            matches -= _secret_matches(
                _baseline_text(worktree, baseline_ref, relative)
            )
        if matches:
            raise VerificationError(f"secret-like content detected: {relative}")


def run_verification(
    worktree: Path,
    config: ProjectConfig,
    profile: str,
    *,
    timeout_seconds: float,
    codex_path: Path | None = None,
    codex_home: Path | None = None,
    control_callback: Callable[[], str | None] | None = None,
) -> VerificationResult:
    if profile not in config.verification:
        raise VerificationError(f"unknown verification profile: {profile}")
    return run_commands(
        worktree,
        config.verification[profile],
        timeout_seconds=timeout_seconds,
        codex_path=codex_path,
        codex_home=codex_home,
        control_callback=control_callback,
    )


def run_commands(
    worktree: Path,
    commands: tuple[str, ...],
    *,
    timeout_seconds: float,
    codex_path: Path | None = None,
    codex_home: Path | None = None,
    permission_profile: str = "codex-worker",
    control_callback: Callable[[], str | None] | None = None,
) -> VerificationResult:
    results: list[CommandResult] = []
    started = time.monotonic()
    termination_reason: str | None = None
    uses_preparation_cache = (
        codex_path is not None and permission_profile == "codex-worker-preparation"
    )
    cache_context = (
        tempfile.TemporaryDirectory(prefix="codex-worker-packages-")
        if uses_preparation_cache
        else nullcontext(None)
    )
    with cache_context as cache_root:
        for command in commands:
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                results.append(CommandResult(command, 124, "verification timed out"))
                break
            argv = ["/bin/zsh", "-lc", command]
            environment: dict[str, str] | None = None
            if codex_path is not None or codex_home is not None:
                if codex_path is None or codex_home is None:
                    raise VerificationError(
                        "codex_path and codex_home must be supplied together"
                    )
                argv = [
                    str(codex_path),
                    "sandbox",
                    "-P",
                    permission_profile,
                    "-C",
                    str(worktree),
                    "--",
                    *argv,
                ]
                environment = {
                    key: value
                    for key, value in os.environ.items()
                    if key in {"PATH", "TMPDIR", "LANG", "LC_ALL"}
                    or key.startswith("LC_")
                }
                environment["HOME"] = os.environ.get("HOME", str(Path.home()))
                environment["CODEX_HOME"] = str(codex_home)
                if cache_root is not None:
                    package_cache = Path(cache_root)
                    pip_cache = package_cache / "pip"
                    npm_cache = package_cache / "npm"
                    pip_cache.mkdir(exist_ok=True)
                    npm_cache.mkdir(exist_ok=True)
                    environment["PIP_CACHE_DIR"] = str(pip_cache)
                    environment["npm_config_cache"] = str(npm_cache)
            try:
                with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output_file:
                    process = subprocess.Popen(
                        argv,
                        cwd=worktree,
                        env=environment,
                        text=True,
                        stdout=output_file,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    deadline = time.monotonic() + max(0.01, remaining)
                    timed_out = False
                    while process.poll() is None:
                        requested = (
                            control_callback() if control_callback is not None else None
                        )
                        if requested in {"pause", "cancel"}:
                            termination_reason = requested
                            os.killpg(process.pid, signal.SIGTERM)
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                os.killpg(process.pid, signal.SIGKILL)
                                process.wait(timeout=2)
                            break
                        if time.monotonic() >= deadline:
                            timed_out = True
                            os.killpg(process.pid, signal.SIGTERM)
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                os.killpg(process.pid, signal.SIGKILL)
                                process.wait(timeout=2)
                            break
                        time.sleep(0.1)
                    process.wait()
                    output_file.seek(0)
                    output = output_file.read()
                    if termination_reason:
                        result = CommandResult(
                            command, 130, output + f"\n{termination_reason} requested"
                        )
                    elif timed_out:
                        result = CommandResult(
                            command, 124, output + "\nverification timed out"
                        )
                    else:
                        result = CommandResult(command, process.returncode, output)
            except OSError as exc:
                result = CommandResult(command, 126, str(exc))
            results.append(result)
            if result.exit_code != 0 or termination_reason:
                break
    return VerificationResult(
        passed=all(item.exit_code == 0 for item in results),
        commands=tuple(results),
        termination_reason=termination_reason,
    )
