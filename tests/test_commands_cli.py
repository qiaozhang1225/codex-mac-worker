from __future__ import annotations

from pathlib import Path

import pytest

from codex_mac_worker.control import create_task, parse_issue_reference, send_command
from codex_mac_worker.coordination import paths_overlap
from codex_mac_worker.protocol import ProtocolError, parse_command_comment, render_command_comment

from .test_protocol import VALID_SHA


class FakeGitHub:
    def __init__(self, issues: list[dict] | None = None) -> None:
        self.created: tuple | None = None
        self.comment: tuple | None = None
        self.issues = issues or []

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> dict:
        self.created = (repo, title, body, labels)
        return {"number": 12, "html_url": "https://github.com/owner/repo/issues/12"}

    def add_comment(self, repo: str, issue_number: int, body: str) -> dict:
        self.comment = (repo, issue_number, body)
        return {"id": 99}

    def list_issues(self, repo: str, *, state: str = "open") -> list[dict]:
        return self.issues


def write_spec(path: Path) -> None:
    path.write_text(
        f"""
schema_version: 1
context_commit: {VALID_SHA}
base_branch: main
objective: Implement one bounded change
acceptance:
  - Tests pass
context_files:
  - docs/spec.md
allowed_paths:
  - src/
verification_profile: fast
risk: low
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_render_and_parse_command_comment() -> None:
    body = render_command_comment(
        action="revise",
        issue_number=12,
        requirements=("Add empty-state test",),
        command_id="cmd-123",
    )

    command = parse_command_comment(body)

    assert command.command_id == "cmd-123"
    assert command.action == "revise"
    assert command.requirements == ("Add empty-state test",)


def test_command_parser_rejects_unsupported_action() -> None:
    body = render_command_comment(
        action="pause",
        issue_number=12,
        requirements=(),
        command_id="cmd-123",
    ).replace("action: pause", "action: merge")

    with pytest.raises(ProtocolError, match="unsupported"):
        parse_command_comment(body)


def test_create_task_posts_valid_machine_block(tmp_path: Path) -> None:
    spec = tmp_path / "task.yaml"
    write_spec(spec)
    github = FakeGitHub()

    result = create_task(github, "owner/repo", "Bounded task", spec)

    assert result["number"] == 12
    assert github.created is not None
    assert github.created[3] == ["codex:queued"]
    assert "<!-- codex-task:v1 -->" in github.created[2]


def test_create_task_derives_title_from_objective(tmp_path: Path) -> None:
    spec = tmp_path / "task.yaml"
    write_spec(spec)
    github = FakeGitHub()

    create_task(github, "owner/repo", None, spec)

    assert github.created is not None
    assert github.created[1] == "[Codex] Implement one bounded change"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (("src/profile/",), ("src/profile/Profile.vue",)),
        (("src/profile/Profile.vue",), ("src/profile/Profile.vue",)),
        (("src/",), ("src",)),
        (("src/old.py", "src/new.py"), ("src/new.py",)),
    ],
)
def test_paths_overlap_by_repository_prefix(
    left: tuple[str, ...], right: tuple[str, ...]
) -> None:
    assert paths_overlap(left, right) is True


def test_paths_overlap_distinguishes_sibling_paths() -> None:
    assert paths_overlap(("src/profile/",), ("src/payments/",)) is False


@pytest.mark.parametrize("path", ["/src", "../src", "src/../secret", r"src\\file"])
def test_paths_overlap_rejects_unsafe_repository_paths(path: str) -> None:
    with pytest.raises(ValueError, match="repository path"):
        paths_overlap((path,), ("src/",))


def active_issue(*, label: str = "codex:running", body: str | None = None) -> dict:
    task = f"""<!-- codex-task:v1 -->
```yaml
schema_version: 1
context_commit: {VALID_SHA}
base_branch: main
objective: Existing bounded task
acceptance:
  - Tests pass
context_files:
  - docs/spec.md
allowed_paths:
  - src/profile/
verification_profile: fast
risk: low
```
"""
    return {
        "number": 10,
        "html_url": "https://github.com/owner/repo/issues/10",
        "body": task if body is None else body,
        "labels": [{"name": label}],
    }


def test_create_task_rejects_active_path_owner(tmp_path: Path) -> None:
    spec = tmp_path / "task.yaml"
    write_spec(spec)
    spec.write_text(
        spec.read_text(encoding="utf-8").replace("  - src/", "  - src/profile/Profile.vue"),
        encoding="utf-8",
    )
    github = FakeGitHub([active_issue()])

    with pytest.raises(ValueError, match="conflicts with active Worker task"):
        create_task(github, "owner/repo", None, spec)

    assert github.created is None


@pytest.mark.parametrize("label", ["codex:completed", "codex:cancelled"])
def test_create_task_ignores_terminal_path_owner(tmp_path: Path, label: str) -> None:
    spec = tmp_path / "task.yaml"
    write_spec(spec)
    github = FakeGitHub([active_issue(label=label)])

    create_task(github, "owner/repo", None, spec)

    assert github.created is not None


def test_create_task_fails_closed_for_malformed_active_worker_issue(tmp_path: Path) -> None:
    spec = tmp_path / "task.yaml"
    write_spec(spec)
    github = FakeGitHub([active_issue(body="malformed task body")])

    with pytest.raises(ValueError, match="conflicts with active Worker task"):
        create_task(github, "owner/repo", None, spec)

    assert github.created is None


def test_send_command_posts_unique_machine_comment(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "requirements:\n  - Add test\n  - Keep API stable\n",
        encoding="utf-8",
    )
    github = FakeGitHub()

    send_command(github, "owner/repo", 12, "revise", requirements)

    assert github.comment is not None
    parsed = parse_command_comment(github.comment[2])
    assert parsed.requirements == ("Add test", "Keep API stable")
    assert parsed.command_id


def test_parse_issue_reference_accepts_url_and_owner_repo_number() -> None:
    assert parse_issue_reference("https://github.com/owner/repo/issues/12") == ("owner/repo", 12)
    assert parse_issue_reference("owner/repo#12") == ("owner/repo", 12)
