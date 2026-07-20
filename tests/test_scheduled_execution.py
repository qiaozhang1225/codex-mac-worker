from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from typing import cast

import pytest

from duomac_contracts import ContractError, TaskSpec, parse_issue_body
from duomac_github import IssueEvent
from duomac_scheduled import (
    ActiveTask,
    Candidate,
    load_scheduled_config,
    paths_overlap,
    select_candidate,
    select_candidate_result,
)
from tests.test_contracts import LEGACY_BODY, VALID_BODY


SCRIPTS = Path(__file__).parents[1] / "skills" / "dual-mac-collaboration" / "scripts"
READY_LABELS = ("duomac:ready",)


def write_config(tmp_path: Path, *, maximum: int = 3) -> Path:
    first = tmp_path / "EaseWise"
    second = tmp_path / "codex-mac-worker"
    first.mkdir()
    second.mkdir()
    path = tmp_path / "repositories.toml"
    path.write_text(
        f'''schema_version = 1
max_parallel_tasks = {maximum}
poll_interval_minutes = 10

[[repositories]]
github = "qiaozhang1225/EaseWise"
local_path = "{first}"

[[repositories]]
github = "qiaozhang1225/codex-mac-worker"
local_path = "{second}"
''',
        encoding="utf-8",
    )
    return path


def test_loads_two_repository_targets(tmp_path: Path) -> None:
    config = load_scheduled_config(write_config(tmp_path))

    assert config.max_parallel_tasks == 3
    assert [item.github for item in config.repositories] == [
        "qiaozhang1225/EaseWise",
        "qiaozhang1225/codex-mac-worker",
    ]


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (("product/frontend",), ("product/frontend/src",), True),
        (("README.md",), ("product/frontend",), False),
        (("product",), ("productivity",), False),
        (("Product/Frontend",), ("product/frontend/src",), True),
        (("product//frontend",), ("docs",), True),
        ((), ("docs",), True),
    ],
)
def test_path_overlap(left: tuple[str, ...], right: tuple[str, ...], expected: bool) -> None:
    assert paths_overlap(left, right) is expected


def test_selection_skips_same_repo_overlap_but_allows_other_repo() -> None:
    spec = parse_issue_body(VALID_BODY)
    ready = (
        Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", spec, labels=READY_LABELS),
        Candidate("owner/other", "https://github.com/owner/other/issues/2", "2026-01-02T00:00:00Z", spec, labels=READY_LABELS),
    )
    active = (ActiveTask("owner/repo", ("product/frontend",)),)

    selected = select_candidate(ready, active, max_parallel_tasks=3)

    assert selected is not None
    assert selected.issue_url.endswith("/issues/2")


def test_selection_treats_case_variants_as_the_same_repository() -> None:
    spec = parse_issue_body(VALID_BODY)
    ready = (
        Candidate("OWNER/REPO", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", spec, labels=READY_LABELS),
    )
    active = (ActiveTask("owner/repo", ("product/frontend",)),)

    assert select_candidate(ready, active, max_parallel_tasks=3) is None


def task_start_event(revision: int) -> IssueEvent:
    return IssueEvent(
        comment_id="IC_start",
        created_at="2026-01-01T00:00:00Z",
        payload={
            "type": "task-start",
            "revision": revision,
            "skill_commit": "a" * 40,
            "base_commit": "b" * 40,
            "plan_summary": ["Start the task"],
            "execution_mode": "scheduled",
            "slot": 1,
            "claim_id": "c" * 40,
        },
    )


@pytest.mark.parametrize(
    ("candidate", "rejection"),
    [
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(LEGACY_BODY)),
            "schema-version",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(VALID_BODY)),
            "missing-ready-label",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(VALID_BODY), labels=("duomac:active",)),
            "wrong-dispatch-label",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(VALID_BODY), labels=("duomac:blocked",)),
            "terminal-state",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(VALID_BODY), labels=READY_LABELS, events=(task_start_event(2),)),
            "already-claimed",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(VALID_BODY), state="cancelled"),
            "terminal-state",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(VALID_BODY), state="blocked"),
            "terminal-state",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", parse_issue_body(VALID_BODY), state="completed"),
            "terminal-state",
        ),
        (
            Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", cast(TaskSpec, object())),
            "invalid-spec",
        ),
    ],
)
def test_selection_skips_ineligible_candidates_with_explicit_reason(
    candidate: Candidate, rejection: str
) -> None:
    result = select_candidate_result((candidate,), (), max_parallel_tasks=3)

    assert result.candidate is None
    assert result.reason == "invalid-candidates-blocked"
    assert result.skipped[0].reason == rejection
    assert select_candidate((candidate,), (), max_parallel_tasks=3) is None


def test_selection_skips_malformed_candidates_without_preventing_a_valid_pick() -> None:
    spec = parse_issue_body(VALID_BODY)
    valid = Candidate("owner/repo", "https://github.com/owner/repo/issues/2", "2026-01-02T00:00:00Z", spec, labels=READY_LABELS)

    result = select_candidate_result((cast(Candidate, object()), valid), (), 3)

    assert result.candidate == valid
    assert result.reason == "selected"
    assert result.skipped[0].reason == "malformed-candidate"


@pytest.mark.parametrize(
    ("events", "rejection"),
    [
        ((IssueEvent("IC_bad", "2026-01-01T00:00:00Z", {"type": "task-start", "revision": "2"}),), "invalid-events"),
        ((IssueEvent("IC_bad", "2026-01-01T00:00:00Z", {"type": "blocked", "revision": 2}),), "invalid-events"),
        ((IssueEvent("IC_bad", "2026-01-01T00:00:00Z", {"type": "delivery", "revision": 2}),), "invalid-events"),
        ((task_start_event(2), task_start_event(2)), "invalid-current-revision-claim"),
    ],
)
def test_selection_fails_closed_for_malformed_or_ambiguous_current_events(
    events: tuple[IssueEvent, ...], rejection: str
) -> None:
    candidate = Candidate(
        "owner/repo",
        "https://github.com/owner/repo/issues/1",
        "2026-01-01T00:00:00Z",
        parse_issue_body(VALID_BODY),
        labels=READY_LABELS,
        events=events,
    )

    result = select_candidate_result((candidate,), (), 3)

    assert result.candidate is None
    assert result.skipped[0].reason == rejection


def test_selection_rejects_spec_that_cannot_be_rendered() -> None:
    spec = replace(
        parse_issue_body(VALID_BODY),
        decisions=cast(tuple[str, ...], (object(),)),
    )
    candidate = Candidate(
        "owner/repo",
        "https://github.com/owner/repo/issues/1",
        "2026-01-01T00:00:00Z",
        spec,
        labels=READY_LABELS,
    )

    result = select_candidate_result((candidate,), (), 3)

    assert result.candidate is None
    assert result.skipped[0].reason == "invalid-spec"


@pytest.mark.parametrize(
    "active",
    [
        (ActiveTask("", ("docs",)),),
        (ActiveTask("owner/other", ("product//frontend",)),),
    ],
)
def test_selection_rejects_invalid_active_tasks_before_repository_comparison(
    active: tuple[ActiveTask, ...]
) -> None:
    candidate = Candidate(
        "owner/repo",
        "https://github.com/owner/repo/issues/1",
        "2026-01-01T00:00:00Z",
        parse_issue_body(VALID_BODY),
        labels=READY_LABELS,
    )

    result = select_candidate_result((candidate,), active, 3)

    assert result.candidate is None
    assert result.reason == "invalid-active"


def test_selection_uses_creation_time_then_issue_url_order() -> None:
    spec = parse_issue_body(VALID_BODY)
    ready = (
        Candidate("owner/repo", "https://github.com/owner/repo/issues/20", "2026-01-02T00:00:00Z", spec, labels=READY_LABELS),
        Candidate("owner/repo", "https://github.com/owner/repo/issues/11", "2026-01-01T00:00:00Z", spec, labels=READY_LABELS),
        Candidate("owner/repo", "https://github.com/owner/repo/issues/10", "2026-01-01T00:00:00Z", spec, labels=READY_LABELS),
    )

    selected = select_candidate(ready, (), max_parallel_tasks=3)

    assert selected is not None
    assert selected.issue_url.endswith("/issues/10")


def test_selection_stops_at_parallel_limit() -> None:
    spec = parse_issue_body(VALID_BODY)
    ready = (Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", spec),)
    active = tuple(ActiveTask(f"owner/repo-{index}", ("README.md",)) for index in range(3))

    assert select_candidate(ready, active, max_parallel_tasks=3) is None


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("schema_version = 1", "schema_version = 2", "schema_version"),
        ("schema_version = 1", "schema_version = true", "schema_version"),
        ("max_parallel_tasks = 3", "max_parallel_tasks = 0", "max_parallel_tasks"),
        ("max_parallel_tasks = 3", "max_parallel_tasks = 2", "max_parallel_tasks"),
        ("max_parallel_tasks = 3", "max_parallel_tasks = true", "max_parallel_tasks"),
        ("poll_interval_minutes = 10", "poll_interval_minutes = 61", "poll_interval_minutes"),
        ("poll_interval_minutes = 10", "poll_interval_minutes = 15", "poll_interval_minutes"),
        ('github = "qiaozhang1225/EaseWise"', 'github = "not a repo"', "github"),
    ],
)
def test_rejects_invalid_scheduled_config_values(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    path = write_config(tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    with pytest.raises(ContractError, match=message):
        load_scheduled_config(path)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("unexpected = true\n", "unknown scheduled config fields"),
        ("\nunknown = true\n", "unknown repository fields"),
    ],
)
def test_rejects_unknown_scheduled_config_fields(
    tmp_path: Path, extra: str, message: str
) -> None:
    path = write_config(tmp_path)
    if "repository" in message:
        content = path.read_text(encoding="utf-8").replace(
            'github = "qiaozhang1225/EaseWise"\n',
            'github = "qiaozhang1225/EaseWise"\nunknown = true\n',
        )
    else:
        content = extra + path.read_text(encoding="utf-8")
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ContractError, match=message):
        load_scheduled_config(path)


def test_rejects_relative_local_repository_path(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(str(tmp_path / "EaseWise"), "EaseWise"),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="absolute path"):
        load_scheduled_config(path)


def test_config_validator_only_reads_configuration(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    before = path.read_bytes()
    entries_before = sorted(item.name for item in tmp_path.iterdir())

    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "config_validate.py"), "--config", str(path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["valid"] is True
    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == entries_before


def test_rejects_duplicate_resolved_repository_paths(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    content = path.read_text(encoding="utf-8")
    first = tmp_path / "EaseWise"
    path.write_text(
        content.replace(str(tmp_path / "codex-mac-worker"), str(first / ".." / first.name)),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="local paths must be unique"):
        load_scheduled_config(path)


def test_rejects_duplicate_repository_names_case_insensitively(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "qiaozhang1225/codex-mac-worker", "QIAOZHANG1225/EASEWISE"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ContractError, match="GitHub names must be unique"):
        load_scheduled_config(path)
