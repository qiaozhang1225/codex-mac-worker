from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol
import uuid

import yaml

from .coordination import active_task_conflicts
from .protocol import TASK_MARKER, parse_task_body, render_command_comment
from .references import parse_issue_reference as parse_issue


class ControlGitHub(Protocol):
    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> dict[str, Any]: ...

    def add_comment(self, repo: str, issue_number: int, body: str) -> dict[str, Any]: ...

    def list_issues(self, repo: str, *, state: str = "open") -> list[dict[str, Any]]: ...


def create_task(
    github: ControlGitHub,
    repo: str,
    title: str | None,
    spec_path: Path,
) -> dict[str, Any]:
    machine_yaml = spec_path.read_text(encoding="utf-8").strip()
    provisional = f"{TASK_MARKER}\n```yaml\n{machine_yaml}\n```\n"
    spec = parse_task_body(provisional)
    conflicts = active_task_conflicts(github, repo, spec.allowed_paths)
    if conflicts:
        raise ValueError(
            "task allowed_paths conflicts with active Worker task: "
            + ", ".join(conflicts)
        )
    issue_title = title or f"[Codex] {spec.objective[:160]}"
    body = f"{issue_title}\n\n{provisional}"
    return github.create_issue(repo, issue_title, body, ["codex:queued"])


def send_command(
    github: ControlGitHub,
    repo: str,
    issue_number: int,
    action: str,
    requirements_path: Path | None = None,
) -> dict[str, Any]:
    requirements: tuple[str, ...] = ()
    if requirements_path is not None:
        text = requirements_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
        if isinstance(raw, dict):
            raw = raw.get("requirements")
        if isinstance(raw, list) and all(isinstance(item, str) and item.strip() for item in raw):
            requirements = tuple(item.strip() for item in raw)
        elif isinstance(raw, str):
            requirements = tuple(line.strip() for line in raw.splitlines() if line.strip())
        else:
            raise ValueError("revision file must contain a requirements string list")
    body = render_command_comment(
        action=action,
        issue_number=issue_number,
        requirements=requirements,
        command_id=str(uuid.uuid4()),
    )
    return github.add_comment(repo, issue_number, body)


def parse_issue_reference(reference: str) -> tuple[str, int]:
    parsed = parse_issue(reference)
    return parsed.repo, parsed.number
