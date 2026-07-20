from __future__ import annotations

from pathlib import Path

import pytest

from duomac_contracts import ContractError, parse_issue_body
from duomac_scheduled import (
    ActiveTask,
    Candidate,
    load_scheduled_config,
    paths_overlap,
    select_candidate,
)
from tests.test_contracts import VALID_BODY


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
        (("product//frontend",), ("docs",), True),
        ((), ("docs",), True),
    ],
)
def test_path_overlap(left: tuple[str, ...], right: tuple[str, ...], expected: bool) -> None:
    assert paths_overlap(left, right) is expected


def test_selection_skips_same_repo_overlap_but_allows_other_repo() -> None:
    spec = parse_issue_body(VALID_BODY)
    ready = (
        Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", spec),
        Candidate("owner/other", "https://github.com/owner/other/issues/2", "2026-01-02T00:00:00Z", spec),
    )
    active = (ActiveTask("owner/repo", ("product/frontend",)),)

    selected = select_candidate(ready, active, max_parallel_tasks=3)

    assert selected is not None
    assert selected.issue_url.endswith("/issues/2")


def test_selection_treats_case_variants_as_the_same_repository() -> None:
    spec = parse_issue_body(VALID_BODY)
    ready = (
        Candidate("OWNER/REPO", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", spec),
    )
    active = (ActiveTask("owner/repo", ("product/frontend",)),)

    assert select_candidate(ready, active, max_parallel_tasks=3) is None


def test_selection_uses_creation_time_then_issue_url_order() -> None:
    spec = parse_issue_body(VALID_BODY)
    ready = (
        Candidate("owner/repo", "https://github.com/owner/repo/issues/20", "2026-01-02T00:00:00Z", spec),
        Candidate("owner/repo", "https://github.com/owner/repo/issues/11", "2026-01-01T00:00:00Z", spec),
        Candidate("owner/repo", "https://github.com/owner/repo/issues/10", "2026-01-01T00:00:00Z", spec),
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
        ("max_parallel_tasks = 3", "max_parallel_tasks = 0", "max_parallel_tasks"),
        ("max_parallel_tasks = 3", "max_parallel_tasks = true", "max_parallel_tasks"),
        ("poll_interval_minutes = 10", "poll_interval_minutes = 61", "poll_interval_minutes"),
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
