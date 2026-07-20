from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import shutil
import subprocess
from typing import Any

import yaml


STATUS_LABELS = (
    "duomac:ready",
    "duomac:active",
    "duomac:blocked",
    "duomac:delivered",
    "duomac:completed",
    "duomac:cancelled",
)
EVENT_MARKER = "<!-- duomac-event:v1 -->"
_EVENT_BLOCK = re.compile(
    re.escape(EVENT_MARKER) + r"\s*```yaml\s*\n(?P<yaml>.*?)\n```",
    re.DOTALL,
)
_ISSUE_URL = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/issues/(?P<number>[1-9][0-9]*)/?$"
)
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GhError(RuntimeError):
    """Raised when a deterministic gh operation cannot complete."""


@dataclass(frozen=True, slots=True)
class IssueRef:
    repo: str
    number: int

    @classmethod
    def parse(cls, value: str) -> "IssueRef":
        match = _ISSUE_URL.fullmatch(value.strip())
        if match is None:
            raise GhError("value must be a full GitHub Issue URL")
        return cls(
            repo=f"{match.group('owner')}/{match.group('repo')}",
            number=int(match.group("number")),
        )

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/issues/{self.number}"


@dataclass(frozen=True, slots=True)
class IssueEvent:
    comment_id: str
    created_at: str
    payload: dict[str, Any]


def parse_issue_events(
    comments: tuple[dict[str, Any], ...],
) -> tuple[IssueEvent, ...]:
    events: list[IssueEvent] = []
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str) or EVENT_MARKER not in body:
            continue
        if body.count(EVENT_MARKER) != 1:
            raise GhError("duomac event marker must be followed by one YAML block")
        match = _EVENT_BLOCK.search(body)
        if match is None:
            raise GhError("duomac event marker must be followed by one YAML block")
        try:
            payload = yaml.safe_load(match.group("yaml"))
        except yaml.YAMLError as exc:
            raise GhError(f"duomac event payload contains invalid YAML: {exc}") from exc
        if not isinstance(payload, dict):
            raise GhError("duomac event payload must be a mapping")
        events.append(
            IssueEvent(
                str(comment.get("id", "")),
                str(comment.get("createdAt", "")),
                payload,
            )
        )
    return tuple(events)


def current_revision_events(
    events: tuple[IssueEvent, ...], revision: int
) -> tuple[IssueEvent, ...]:
    return tuple(event for event in events if event.payload.get("revision") == revision)


class GhClient:
    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("gh") or ""

    def _run(self, args: list[str], *, stdin: str | None = None) -> str:
        if not self.executable:
            raise GhError("gh CLI was not found on PATH")
        env = os.environ.copy()
        env["GH_PROMPT_DISABLED"] = "1"
        try:
            result = subprocess.run(
                [self.executable, *args],
                input=stdin,
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
        except OSError as exc:
            raise GhError(f"unable to run gh CLI: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown gh error"
            raise GhError(f"gh command failed: {detail}")
        return result.stdout.strip()

    def _json(self, args: list[str]) -> dict[str, Any]:
        raw = self._run(args)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GhError("gh returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise GhError("gh returned an unexpected JSON value")
        return value

    def issue_body(self, ref: IssueRef) -> str:
        value = self._json(["issue", "view", ref.url, "--json", "body"])
        body = value.get("body")
        if not isinstance(body, str):
            raise GhError("GitHub Issue body is missing")
        return body

    def issue_comments(self, ref: IssueRef) -> tuple[dict[str, Any], ...]:
        value = self._json(["issue", "view", ref.url, "--json", "comments"])
        comments = value.get("comments")
        if not isinstance(comments, list) or not all(
            isinstance(item, dict) for item in comments
        ):
            raise GhError("GitHub Issue comments have an unexpected shape")
        return tuple(comments)

    def create_issue(self, repo: str, title: str, body: str) -> str:
        if _REPOSITORY.fullmatch(repo) is None:
            raise GhError("repository must use OWNER/REPO format")
        output = self._run(
            [
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--body-file",
                "-",
            ],
            stdin=body,
        )
        return IssueRef.parse(output).url

    def edit_body(self, ref: IssueRef, body: str) -> None:
        self._run(["issue", "edit", ref.url, "--body-file", "-"], stdin=body)

    def comment(self, ref: IssueRef, body: str) -> None:
        self._run(["issue", "comment", ref.url, "--body-file", "-"], stdin=body)

    def _labels(self, ref: IssueRef) -> tuple[str, ...]:
        value = self._json(["issue", "view", ref.url, "--json", "labels"])
        labels = value.get("labels")
        if not isinstance(labels, list):
            raise GhError("GitHub Issue labels are missing")
        names: list[str] = []
        for label in labels:
            if not isinstance(label, dict) or not isinstance(label.get("name"), str):
                raise GhError("GitHub Issue labels have an unexpected shape")
            names.append(label["name"])
        return tuple(names)

    def set_state_label(self, ref: IssueRef, label: str) -> None:
        if label not in STATUS_LABELS:
            raise GhError(f"unknown dual-Mac state label: {label}")
        current = self._labels(ref)
        args = ["issue", "edit", ref.url]
        for old in current:
            if old in STATUS_LABELS and old != label:
                args.extend(["--remove-label", old])
        args.extend(["--add-label", label])
        self._run(args)

    def close(self, ref: IssueRef) -> None:
        self._run(["issue", "close", ref.url])
