# Scheduled Dual-Mac Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade `dual-mac-collaboration` so Codex App Scheduled runs can safely claim and execute up to three non-overlapping GitHub Issue tasks while completion is mechanically blocked until every schema v2 milestone has a checkpoint.

**Architecture:** Keep GitHub Issue bodies and events authoritative. Add schema v2 structured milestones, event-history validation, and a preview-first scheduled picker protected by a Mac-local `fcntl` lock; Codex App remains the only scheduler and every claimed Issue runs in its own visible Scheduled conversation and skill-managed worktree.

**Tech Stack:** Python 3.12, PyYAML, `tomllib`, `fcntl`, `gh` CLI, Git, pytest, zsh installer, Codex App Scheduled tasks.

## Global Constraints

- Do not use Codex Goal mode, `codex exec`, a Worker daemon, LaunchDaemon, or an external polling loop.
- MacBook remains the only dispatcher; formal Issue creation still requires explicit user confirmation after the complete contract is shown.
- New tasks use schema v2; closed schema v1 Issues remain readable, but a ready v1 Issue cannot be scheduled.
- Mac mini reads repositories and `max_parallel_tasks = 3` from local configuration; the skill must not hard-code repository access policy.
- Each Scheduled run claims at most one Issue, uses an isolated `codex/*` worktree, and never force pushes or deploys.
- Same-repository tasks may run in parallel only when their `allowed_paths` do not overlap; different repositories may run in parallel.
- Verification commands continue to come only from each repository's `.duomac/project.toml`.
- GitHub task-start is the authoritative claim event; labels and local claim files are projections that must be repairable without duplicate execution.
- Use preview-first behavior for every new write-capable CLI.

---

## File Map

- Modify `skills/dual-mac-collaboration/scripts/duomac_contracts.py` — parse and render schema v1/v2 contracts and expose structured milestones.
- Modify `skills/dual-mac-collaboration/scripts/duomac_github.py` — fetch Issue summaries/comments, parse events, and support idempotent label repair.
- Modify `skills/dual-mac-collaboration/scripts/issue_checkpoint.py` — validate task-start audit fields and strict checkpoint ordering.
- Modify `skills/dual-mac-collaboration/scripts/issue_complete.py` — require the full current-revision checkpoint set before delivery.
- Create `skills/dual-mac-collaboration/scripts/duomac_scheduled.py` — local config, path-overlap, candidate-selection, Git context validation, and lock primitives.
- Create `skills/dual-mac-collaboration/scripts/config_validate.py` — preview local Scheduled configuration as JSON.
- Create `skills/dual-mac-collaboration/scripts/scheduled_pick.py` — preview or claim one eligible Issue.
- Create `skills/dual-mac-collaboration/references/scheduled-execution.md` — Scheduled-mode workflow and stop conditions.
- Create `skills/dual-mac-collaboration/assets/scheduled-slot-prompt.md` — exact prompt copied into each Codex App Scheduled task.
- Create `skills/dual-mac-collaboration/assets/repositories.toml.example` — approved two-repository Mac mini configuration example.
- Modify `skills/dual-mac-collaboration/SKILL.md`, `references/issue-protocol.md`, `references/checkpoints.md`, `agents/openai.yaml`, and root `README.md` — route Scheduled mode and document schema v2.
- Modify `scripts/install_skill.sh` — install two new wrappers and a non-overwriting config example.
- Modify `tests/test_contracts.py`, `tests/test_issue_commands.py`, `tests/test_git_delivery.py`, `tests/test_skill_content.py`, and `tests/skill_scenarios.yaml` — update existing fixtures and enforcement.
- Create `tests/test_scheduled_execution.py` — local config, selection, locking, and claim tests.

---

### Task 1: Add Structured Schema v2 Milestones

**Files:**
- Modify: `tests/test_contracts.py`
- Modify: `tests/test_git_delivery.py`
- Modify: `skills/dual-mac-collaboration/scripts/duomac_contracts.py`

**Interfaces:**
- Produces: `Milestone(number: int, objective: str, steps: tuple[str, ...])`
- Produces: `TaskSpec.schema_version: Literal[1, 2]`
- Produces: `TaskSpec.execution_plan: tuple[Milestone, ...]`
- Produces: `require_current_schema(spec: TaskSpec) -> None`
- Preserves: schema v1 parse/render for historical read-only inspection.

- [ ] **Step 1: Replace the primary fixture with a schema v2 contract**

Use this exact shape in `tests/test_contracts.py` and `tests/test_issue_commands.py`:

````python
VALID_BODY = """<!-- duomac-task:v1 -->
```yaml
schema_version: 2
revision: 2
role:
  dispatcher: macbook
  executor: mac-mini
objective: Fix the bounded history-card layout
context:
  commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  files:
    - docs/product/product-spec.md
  decisions:
    - Do not change backend behavior
acceptance:
  - The mobile card uses the available width
scope:
  allowed_paths:
    - product/frontend/src/history
  out_of_scope:
    - Backend APIs
execution_plan:
  - milestone: 1
    objective: Update the component
    steps:
      - Apply the bounded layout change
  - milestone: 2
    objective: Verify and deliver the change
    steps:
      - Run the fast profile
      - Publish delivery evidence
verification_profile: fast
delivery_mode: direct-main
risk: low
```
"""
````

- [ ] **Step 2: Add failing contract tests**

Add:

```python
LEGACY_BODY = VALID_BODY.replace(
    "schema_version: 2",
    "schema_version: 1",
).replace(
    "execution_plan:\n  - milestone: 1\n    objective: Update the component\n    steps:\n      - Apply the bounded layout change\n  - milestone: 2\n    objective: Verify and deliver the change\n    steps:\n      - Run the fast profile\n      - Publish delivery evidence",
    "execution_plan:\n  - Update the component\n  - Run the fast profile",
)


def test_parses_schema_v2_milestones() -> None:
    spec = parse_issue_body(VALID_BODY)

    assert spec.schema_version == 2
    assert [item.number for item in spec.execution_plan] == [1, 2]
    assert spec.execution_plan[1].steps == (
        "Run the fast profile",
        "Publish delivery evidence",
    )


def test_rejects_nonconsecutive_milestones() -> None:
    body = VALID_BODY.replace("milestone: 2", "milestone: 3")

    with pytest.raises(ContractError, match="continuous"):
        parse_issue_body(body)


def test_legacy_contract_is_readable_but_not_current() -> None:
    spec = parse_issue_body(LEGACY_BODY)

    assert spec.schema_version == 1
    with pytest.raises(ContractError, match="schema_version 2"):
        require_current_schema(spec)
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_contracts.py::test_parses_schema_v2_milestones tests/test_contracts.py::test_rejects_nonconsecutive_milestones tests/test_contracts.py::test_legacy_contract_is_readable_but_not_current -q
```

Expected: FAIL because `TaskSpec` has no `schema_version`, execution plans are strings, and `require_current_schema` does not exist.

- [ ] **Step 4: Implement schema-aware parsing and rendering**

Add the dataclass and field definitions:

```python
@dataclass(frozen=True, slots=True)
class Milestone:
    number: int
    objective: str
    steps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskSpec:
    schema_version: Literal[1, 2]
    revision: int
    dispatcher: str
    executor: str
    objective: str
    context_commit: str
    context_files: tuple[str, ...]
    decisions: tuple[str, ...]
    acceptance: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    execution_plan: tuple[Milestone, ...]
    verification_profile: str
    delivery_mode: Literal["direct-main", "task-branch"]
    risk: Literal["low", "medium"]
```

Add exact helpers:

```python
def _milestones(task: dict[str, Any], schema_version: int) -> tuple[Milestone, ...]:
    raw = task.get("execution_plan")
    if not isinstance(raw, list) or not raw:
        raise ContractError("execution_plan must be a non-empty list")
    if schema_version == 1:
        if not all(isinstance(item, str) and item.strip() for item in raw):
            raise ContractError("schema v1 execution_plan must be a string list")
        return tuple(
            Milestone(index, item.strip(), (item.strip(),))
            for index, item in enumerate(raw, start=1)
        )
    milestones: list[Milestone] = []
    for expected, item in enumerate(raw, start=1):
        entry = _mapping(item, f"execution_plan[{expected}]")
        number = _positive_int(entry, "milestone")
        if number != expected:
            raise ContractError("execution_plan milestones must be continuous from 1")
        milestones.append(
            Milestone(number, _string(entry, "objective"), _strings(entry, "steps"))
        )
    return tuple(milestones)


def require_current_schema(spec: TaskSpec) -> None:
    if spec.schema_version != 2:
        raise ContractError("scheduled execution requires schema_version 2")
```

Accept only `schema_version in {1, 2}`, pass it into `_milestones`, render v1 as strings and v2 as mappings, and flatten each milestone objective and steps inside `validate_task`.

- [ ] **Step 5: Update all direct `TaskSpec(...)` constructions**

Import `Milestone` and add:

```python
schema_version=2,
execution_plan=(
    Milestone(1, "Update card", ("Edit the card",)),
    Milestone(2, "Verify", ("Run the fast profile",)),
),
```

Replace the old string tuple in `tests/test_git_delivery.py` and any remaining tests found by:

```bash
rg -n 'TaskSpec\(' tests skills
```

- [ ] **Step 6: Run the contract and Git tests**

Run:

```bash
.venv/bin/pytest tests/test_contracts.py tests/test_git_delivery.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add skills/dual-mac-collaboration/scripts/duomac_contracts.py tests/test_contracts.py tests/test_git_delivery.py tests/test_issue_commands.py
git commit -m "feat: add schema v2 milestones"
```

---

### Task 2: Parse GitHub Events and Enforce Checkpoint Order

**Files:**
- Modify: `skills/dual-mac-collaboration/scripts/duomac_github.py`
- Modify: `skills/dual-mac-collaboration/scripts/issue_checkpoint.py`
- Modify: `tests/test_issue_commands.py`

**Interfaces:**
- Produces: `IssueEvent(comment_id: str, created_at: str, payload: dict[str, Any])`
- Produces: `GhClient.issue_comments(ref: IssueRef) -> tuple[dict[str, Any], ...]`
- Produces: `parse_issue_events(comments) -> tuple[IssueEvent, ...]`
- Produces: `current_revision_events(events, revision) -> tuple[IssueEvent, ...]`
- Produces: `apply_event(client, ref, spec, payload) -> dict[str, Any]` for Scheduled claiming and the checkpoint CLI.

- [ ] **Step 1: Extend the fake GitHub fixture**

Add `"comments": []` to the fixture JSON and handle `--json comments`:

```python
elif field == "comments":
    print(json.dumps({"comments": fixture.get("comments", [])}))
```

Add this test helper:

```python
def event_comment(payload: dict[str, object], comment_id: str = "IC_1") -> dict[str, object]:
    rendered = yaml.safe_dump(payload, sort_keys=False).rstrip()
    return {
        "id": comment_id,
        "createdAt": "2026-07-20T00:00:00Z",
        "body": f"<!-- duomac-event:v1 -->\n```yaml\n{rendered}\n```\n",
    }
```

- [ ] **Step 2: Add failing event and checkpoint tests**

Add:

```python
def valid_task_start(*, claim_id: str = "c" * 40) -> dict[str, object]:
    return {
        "type": "task-start",
        "revision": 2,
        "skill_commit": "d" * 40,
        "base_commit": "a" * 40,
        "plan_summary": ["Execute two milestones"],
        "execution_mode": "scheduled",
        "slot": 1,
        "claim_id": claim_id,
    }


def test_checkpoint_rejects_gap_after_task_start(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [event_comment(valid_task_start())]
    fixture.write_text(json.dumps(value), encoding="utf-8")
    payload = valid_checkpoint()
    payload["milestone"] = 2

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        env=cli_env,
    )

    assert result.returncode != 0
    assert "milestone 1" in result.stderr


def test_task_start_is_idempotent_for_same_claim(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    payload = valid_task_start()
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [event_comment(payload)]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_checkpoint.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["repaired"] is True
    assert not any(
        call["argv"][:2] == ["issue", "comment"] for call in gh_calls(cli_env)
    )
```

- [ ] **Step 3: Run the checkpoint tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_issue_commands.py::test_checkpoint_rejects_gap_after_task_start tests/test_issue_commands.py::test_task_start_is_idempotent_for_same_claim -q
```

Expected: FAIL because comments and event order are not read.

- [ ] **Step 4: Add event parsing to `duomac_github.py`**

Add:

```python
_EVENT_BLOCK = re.compile(
    re.escape(EVENT_MARKER) + r"\s*```yaml\s*\n(?P<yaml>.*?)\n```",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class IssueEvent:
    comment_id: str
    created_at: str
    payload: dict[str, Any]


def parse_issue_events(comments: tuple[dict[str, Any], ...]) -> tuple[IssueEvent, ...]:
    events: list[IssueEvent] = []
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str) or EVENT_MARKER not in body:
            continue
        match = _EVENT_BLOCK.search(body)
        if match is None:
            raise GhError("duomac event marker must be followed by one YAML block")
        payload = yaml.safe_load(match.group("yaml"))
        if not isinstance(payload, dict):
            raise GhError("duomac event payload must be a mapping")
        events.append(
            IssueEvent(str(comment.get("id", "")), str(comment.get("createdAt", "")), payload)
        )
    return tuple(events)


def current_revision_events(
    events: tuple[IssueEvent, ...], revision: int
) -> tuple[IssueEvent, ...]:
    return tuple(event for event in events if event.payload.get("revision") == revision)
```

Expose:

```python
def issue_comments(self, ref: IssueRef) -> tuple[dict[str, Any], ...]:
    value = self._json(["issue", "view", ref.url, "--json", "comments"])
    comments = value.get("comments")
    if not isinstance(comments, list) or not all(isinstance(item, dict) for item in comments):
        raise GhError("GitHub Issue comments have an unexpected shape")
    return tuple(comments)
```

- [ ] **Step 5: Enforce task-start and checkpoint sequence**

Change task-start validation to require:

```python
_CLAIM_ID = re.compile(r"^[0-9a-f]{40}$")

if payload.get("execution_mode") not in {"scheduled", "interactive"}:
    raise ContractError("task-start execution_mode must be scheduled or interactive")
if payload["execution_mode"] == "scheduled":
    slot = payload.get("slot")
    claim_id = payload.get("claim_id")
    if not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= 3:
        raise ContractError("scheduled task-start slot must be 1, 2, or 3")
    if not isinstance(claim_id, str) or _CLAIM_ID.fullmatch(claim_id) is None:
        raise ContractError("scheduled task-start claim_id must be 40 lowercase hex characters")
```

Inside `apply_event`, fetch and parse comments. For task-start, return `{"repaired": True}` without commenting when the same revision and claim ID already exists; reject a different claim. For checkpoint, compute existing milestone numbers and require:

```python
expected = len(existing_milestones) + 1
if payload["milestone"] != expected:
    raise ContractError(f"next checkpoint must be milestone {expected}")
if expected > len(spec.execution_plan):
    raise ContractError("all declared milestones already have checkpoints")
```

Publish the event comment before changing its state label. A rerun repairs the label from existing evidence rather than duplicating the event.

- [ ] **Step 6: Run all Issue command tests**

Run:

```bash
.venv/bin/pytest tests/test_issue_commands.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add skills/dual-mac-collaboration/scripts/duomac_github.py skills/dual-mac-collaboration/scripts/issue_checkpoint.py tests/test_issue_commands.py
git commit -m "feat: enforce ordered dual-Mac checkpoints"
```

---

### Task 3: Block Completion Until Every Milestone Is Evidenced

**Files:**
- Modify: `skills/dual-mac-collaboration/scripts/issue_complete.py`
- Modify: `tests/test_issue_commands.py`

**Interfaces:**
- Produces: `validate_checkpoint_completion(spec, events) -> None`
- Strengthens: `validate_delivery` rejects any `acceptance_results.status != "met"`.

- [ ] **Step 1: Add failing completion tests**

Add:

```python
def checkpoint_event(milestone: int) -> dict[str, object]:
    payload = valid_checkpoint()
    payload["milestone"] = milestone
    return event_comment(payload, f"IC_{milestone}")


def test_completion_rejects_missing_final_checkpoint(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [event_comment(valid_task_start()), checkpoint_event(1)]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_delivery()),
        "--state",
        "completed",
        "--yes",
        env=cli_env,
    )

    assert result.returncode != 0
    assert "missing checkpoint milestones: 2" in result.stderr


def test_completion_accepts_all_current_revision_checkpoints(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    fixture = Path(cli_env["GH_FAKE_FIXTURE"])
    value = json.loads(fixture.read_text())
    value["comments"] = [
        event_comment(valid_task_start()),
        checkpoint_event(1),
        checkpoint_event(2),
    ]
    fixture.write_text(json.dumps(value), encoding="utf-8")

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, valid_delivery()),
        "--state",
        "completed",
        "--yes",
        env=cli_env,
    )

    assert result.returncode == 0, result.stderr


def test_completion_rejects_not_met_acceptance(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    payload = valid_delivery()
    payload["acceptance_results"][0]["status"] = "not-met"

    result = run_script(
        "issue_complete.py",
        ISSUE_URL,
        "--payload",
        write_payload(tmp_path, payload),
        "--state",
        "completed",
        env=cli_env,
    )

    assert result.returncode != 0
    assert "all acceptance results must be met" in result.stderr
```

- [ ] **Step 2: Run the completion tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_issue_commands.py::test_completion_rejects_missing_final_checkpoint tests/test_issue_commands.py::test_completion_accepts_all_current_revision_checkpoints tests/test_issue_commands.py::test_completion_rejects_not_met_acceptance -q
```

Expected: first and third tests FAIL because the current CLI accepts incomplete evidence; the second may fail until the fake comment API is wired.

- [ ] **Step 3: Implement the completion gate**

Add:

```python
def validate_checkpoint_completion(
    spec: TaskSpec, events: tuple[IssueEvent, ...]
) -> None:
    require_current_schema(spec)
    current = current_revision_events(events, spec.revision)
    blocked = [event for event in current if event.payload.get("type") == "blocked"]
    if blocked:
        raise ContractError("current revision contains an unresolved blocked event")
    actual = {
        event.payload["milestone"]
        for event in current
        if event.payload.get("type") == "checkpoint"
    }
    expected = set(range(1, len(spec.execution_plan) + 1))
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise ContractError(
            "missing checkpoint milestones: " + ", ".join(map(str, missing))
        )
    if extra:
        raise ContractError(
            "unexpected checkpoint milestones: " + ", ".join(map(str, extra))
        )
```

Call it after parsing the current body and comments but before preview output or any write. Replace the acceptance status condition with:

```python
if result.get("status") != "met":
    raise ContractError("all acceptance results must be met before delivery")
```

- [ ] **Step 4: Run Issue tests**

Run:

```bash
.venv/bin/pytest tests/test_issue_commands.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/dual-mac-collaboration/scripts/issue_complete.py tests/test_issue_commands.py
git commit -m "fix: require every milestone before completion"
```

---

### Task 4: Add Mac mini Repository Configuration and Pure Selection Rules

**Files:**
- Create: `skills/dual-mac-collaboration/scripts/duomac_scheduled.py`
- Create: `skills/dual-mac-collaboration/scripts/config_validate.py`
- Create: `tests/test_scheduled_execution.py`

**Interfaces:**
- Produces: `RepositoryTarget(github: str, local_path: Path)`
- Produces: `ScheduledConfig(max_parallel_tasks: int, poll_interval_minutes: int, repositories: tuple[RepositoryTarget, ...])`
- Produces: `load_scheduled_config(path: Path) -> ScheduledConfig`
- Produces: `paths_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool`
- Produces: `select_candidate(ready, active, max_parallel_tasks) -> Candidate | None`
- Produces: `dispatch_lock(path: Path)` context manager using `fcntl.flock`.

- [ ] **Step 1: Write failing config and selection tests**

Create `tests/test_scheduled_execution.py` with:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from duomac_contracts import ContractError, parse_issue_body
from duomac_scheduled import (
    Candidate,
    ActiveTask,
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


def test_selection_stops_at_parallel_limit() -> None:
    spec = parse_issue_body(VALID_BODY)
    ready = (Candidate("owner/repo", "https://github.com/owner/repo/issues/1", "2026-01-01T00:00:00Z", spec),)
    active = tuple(ActiveTask(f"owner/repo-{index}", ("README.md",)) for index in range(3))

    assert select_candidate(ready, active, max_parallel_tasks=3) is None
```

- [ ] **Step 2: Run the Scheduled unit tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_scheduled_execution.py -q
```

Expected: collection ERROR because `duomac_scheduled` does not exist.

- [ ] **Step 3: Implement config and pure selection**

Create:

```python
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from pathlib import Path
import re
import tomllib
from typing import Iterator, Sequence

from duomac_contracts import ContractError, TaskSpec


_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class RepositoryTarget:
    github: str
    local_path: Path


@dataclass(frozen=True, slots=True)
class ScheduledConfig:
    max_parallel_tasks: int
    poll_interval_minutes: int
    repositories: tuple[RepositoryTarget, ...]


@dataclass(frozen=True, slots=True)
class Candidate:
    repo: str
    issue_url: str
    created_at: str
    spec: TaskSpec


@dataclass(frozen=True, slots=True)
class ActiveTask:
    repo: str
    allowed_paths: tuple[str, ...]


def paths_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return any(
        a == b or a.startswith(b + "/") or b.startswith(a + "/")
        for a in left
        for b in right
    )


def select_candidate(
    ready: Sequence[Candidate],
    active: Sequence[ActiveTask],
    max_parallel_tasks: int,
) -> Candidate | None:
    if len(active) >= max_parallel_tasks:
        return None
    for candidate in sorted(ready, key=lambda item: (item.created_at, item.issue_url)):
        conflict = any(
            item.repo == candidate.repo
            and paths_overlap(item.allowed_paths, candidate.spec.allowed_paths)
            for item in active
        )
        if not conflict:
            return candidate
    return None


@contextmanager
def dispatch_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
```

Implement `load_scheduled_config` with these exact constraints: schema version 1; maximum 1–8; interval 5–60; at least one repository; unique GitHub names and resolved local paths; valid `OWNER/REPO`; each local path must be an existing directory.

- [ ] **Step 4: Implement `config_validate.py`**

Use:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from duomac_contracts import ContractError
from duomac_scheduled import load_scheduled_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Mac mini Scheduled repository configuration")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    try:
        config = load_scheduled_config(args.config)
    except (ContractError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({
        "valid": True,
        "max_parallel_tasks": config.max_parallel_tasks,
        "poll_interval_minutes": config.poll_interval_minutes,
        "repositories": [
            {"github": item.github, "local_path": str(item.local_path)}
            for item in config.repositories
        ],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run Scheduled tests**

Run:

```bash
.venv/bin/pytest tests/test_scheduled_execution.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add skills/dual-mac-collaboration/scripts/duomac_scheduled.py skills/dual-mac-collaboration/scripts/config_validate.py tests/test_scheduled_execution.py
git commit -m "feat: add Scheduled repository configuration"
```

---

### Task 5: Implement Preview-First Atomic Scheduled Claiming

**Files:**
- Modify: `skills/dual-mac-collaboration/scripts/duomac_github.py`
- Modify: `skills/dual-mac-collaboration/scripts/duomac_scheduled.py`
- Create: `skills/dual-mac-collaboration/scripts/scheduled_pick.py`
- Modify: `tests/test_scheduled_execution.py`

**Interfaces:**
- Produces: `GhClient.list_issues(repo: str, label: str) -> tuple[IssueSummary, ...]`
- Produces: `validate_repository_target(target, spec) -> RepositoryEvidence`
- Produces: `pick(config_path, app_root, slot, apply) -> PickResult`
- CLI output always contains `claimed`, `reason`, and, when claimed, `issue_url`, `repo`, `local_path`, `slot`, `claim_id`, `base_commit`.

- [ ] **Step 1: Add failing picker tests**

Extend the fake `gh` fixture to support `issue list`, `comments`, label edits, and comments. Its state-file writes must use `fcntl.flock` so the concurrency test cannot lose an update. Add this complete test-facing fixture interface before the picker tests:

```python
@dataclass
class ScheduledEnv:
    env: dict[str, str]
    config: Path
    app_root: Path
    fixture: Path
    log: Path

    def run_picker(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "scheduled_pick.py"),
                "--config",
                str(self.config),
                "--app-root",
                str(self.app_root),
                *args,
            ],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def state(self) -> dict[str, object]:
        return json.loads(self.fixture.read_text(encoding="utf-8"))

    def write_state(self, value: dict[str, object]) -> None:
        self.fixture.write_text(json.dumps(value), encoding="utf-8")

    def github_writes(self) -> list[dict[str, object]]:
        if not self.log.exists():
            return []
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [
            call for call in calls
            if call["argv"][:2] in (["issue", "edit"], ["issue", "comment"], ["issue", "close"])
        ]

    def task_start_comments(self) -> list[dict[str, object]]:
        comments = self.state()["issues"][0]["comments"]
        return [item for item in comments if "type: task-start" in item["body"]]

    def replace_ready_body(self, body: str) -> None:
        value = self.state()
        value["issues"][0]["body"] = body
        self.write_state(value)

    def add_comment(self, comment: dict[str, object]) -> None:
        value = self.state()
        value["issues"][0]["comments"].append(comment)
        self.write_state(value)

    def set_labels(self, labels: list[str]) -> None:
        value = self.state()
        value["issues"][0]["labels"] = labels
        self.write_state(value)

    def current_label(self) -> str:
        labels = self.state()["issues"][0]["labels"]
        return next(item for item in labels if item.startswith("duomac:"))

    def run_two_pickers_concurrently(
        self, first_slot: int, second_slot: int
    ) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
        command = lambda slot: [
            sys.executable,
            str(SCRIPTS / "scheduled_pick.py"),
            "--config",
            str(self.config),
            "--app-root",
            str(self.app_root),
            "--slot",
            str(slot),
            "--yes",
        ]
        processes = [
            subprocess.Popen(
                command(slot),
                cwd=ROOT,
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for slot in (first_slot, second_slot)
        ]
        completed = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            completed.append(
                subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
            )
        return tuple(completed)
```

Create a `scheduled_env` pytest fixture that builds two real local Git repositories with matching `origin` URLs, commits `.duomac/project.toml` and all context files, writes the exact two-repository config from Task 4, and puts one ready schema v2 Issue into the fake GitHub state. Use the `ScheduledEnv` constructor above as the fixture return value. Do not mock path-overlap or Git ancestry logic.

Then add:

```python
def test_picker_preview_has_no_github_writes(scheduled_env: ScheduledEnv) -> None:
    result = scheduled_env.run_picker("--slot", "1")

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["claimed"] is False
    assert output["reason"] == "preview"
    assert not scheduled_env.github_writes()


def test_picker_claims_oldest_eligible_issue_once(scheduled_env: ScheduledEnv) -> None:
    first = scheduled_env.run_picker("--slot", "1", "--yes")
    second = scheduled_env.run_picker("--slot", "2", "--yes")

    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["claimed"] is True
    assert json.loads(second.stdout)["claimed"] is False
    assert len(scheduled_env.task_start_comments()) == 1


def test_picker_blocks_ready_schema_v1(scheduled_env: ScheduledEnv) -> None:
    scheduled_env.replace_ready_body(LEGACY_BODY)

    result = scheduled_env.run_picker("--slot", "1", "--yes")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["claimed"] is False
    assert scheduled_env.current_label() == "duomac:blocked"


def test_picker_repairs_active_label_after_task_start_comment(
    scheduled_env: ScheduledEnv,
) -> None:
    claim_id = "c" * 40
    scheduled_env.add_comment(event_comment(valid_task_start(claim_id=claim_id)))
    scheduled_env.set_labels(["duomac:ready"])

    result = scheduled_env.run_picker("--slot", "1", "--yes")

    assert result.returncode == 0, result.stderr
    assert scheduled_env.current_label() == "duomac:active"
    assert len(scheduled_env.task_start_comments()) == 1
```

- [ ] **Step 2: Run picker tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_scheduled_execution.py -k picker -q
```

Expected: FAIL because `scheduled_pick.py`, Issue listing, and claiming do not exist.

- [ ] **Step 3: Add Issue listing and repository evidence**

Add to `duomac_github.py`:

```python
@dataclass(frozen=True, slots=True)
class IssueSummary:
    repo: str
    url: str
    created_at: str
    body: str
    labels: tuple[str, ...]


def list_issues(self, repo: str, label: str) -> tuple[IssueSummary, ...]:
    raw = self._run([
        "issue", "list", "--repo", repo, "--state", "open", "--label", label,
        "--limit", "100", "--json", "url,createdAt,body,labels",
    ])
    value = json.loads(raw)
    if not isinstance(value, list):
        raise GhError("gh issue list returned an unexpected value")
    return tuple(
        IssueSummary(
            repo=repo,
            url=item["url"],
            created_at=item["createdAt"],
            body=item["body"],
            labels=tuple(label["name"] for label in item["labels"]),
        )
        for item in value
    )
```

Implement repository validation using non-interactive `git -C`: canonicalize the `origin` URL to `OWNER/REPO`; read the configured `ProjectConfig.default_base_branch`; fetch that branch; require `context_commit` to be an ancestor of `origin/{default_base_branch}`; read `.duomac/project.toml` from the context commit through a new `load_project_config_text` helper; call `validate_task`; return the fetched base commit.

- [ ] **Step 4: Implement `scheduled_pick.py`**

Use this CLI contract:

```python
parser = argparse.ArgumentParser(description="Preview or claim one Scheduled dual-Mac Issue")
parser.add_argument("--config", required=True, type=Path)
parser.add_argument("--app-root", required=True, type=Path)
parser.add_argument("--slot", required=True, type=int, choices=(1, 2, 3))
parser.add_argument("--yes", action="store_true")
```

Inside the local dispatch lock:

1. Fetch active and ready lists for all configured repositories.
2. Parse active v2 bodies into `ActiveTask` values.
3. Convert valid ready bodies into `Candidate` values; collect invalid v1/current-schema errors.
4. Run `select_candidate`.
5. In preview mode, output the candidate and `reason: preview` without any GitHub writes.
6. In apply mode, block invalid ready contracts with structured evidence, then select again.
7. Validate the selected local repository and context.
8. Generate `claim_id = secrets.token_hex(20)` and task-start containing exact installed skill commit, fetched base commit, `execution_mode: scheduled`, `slot`, and `claim_id`.
9. Call `apply_event`; write a diagnostic claim JSON under `claims/` only after GitHub task-start succeeds.

No-candidate reasons must be one of `no-ready`, `parallel-limit`, `path-conflict`, or `invalid-candidates-blocked`.

- [ ] **Step 5: Add the lock pressure test**

Use two subprocesses held at the same barrier and assert exactly one task-start comment:

```python
def test_two_slots_cannot_claim_the_same_issue(scheduled_env: ScheduledEnv) -> None:
    results = scheduled_env.run_two_pickers_concurrently(1, 2)

    assert all(result.returncode == 0 for result in results)
    assert sum(json.loads(result.stdout)["claimed"] for result in results) == 1
    assert len(scheduled_env.task_start_comments()) == 1
```

- [ ] **Step 6: Run Scheduled tests**

Run:

```bash
.venv/bin/pytest tests/test_scheduled_execution.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add skills/dual-mac-collaboration/scripts/duomac_github.py skills/dual-mac-collaboration/scripts/duomac_scheduled.py skills/dual-mac-collaboration/scripts/scheduled_pick.py tests/test_scheduled_execution.py
git commit -m "feat: claim Scheduled dual-Mac Issues atomically"
```

---

### Task 6: Update the Skill, Scheduled Prompt, Installer, and Documentation

**Files:**
- Modify: `tests/test_skill_content.py`
- Modify: `tests/skill_scenarios.yaml`
- Modify: `skills/dual-mac-collaboration/SKILL.md`
- Modify: `skills/dual-mac-collaboration/references/issue-protocol.md`
- Modify: `skills/dual-mac-collaboration/references/checkpoints.md`
- Create: `skills/dual-mac-collaboration/references/scheduled-execution.md`
- Create: `skills/dual-mac-collaboration/assets/scheduled-slot-prompt.md`
- Modify: `skills/dual-mac-collaboration/agents/openai.yaml`
- Modify: `scripts/install_skill.sh`
- Modify: `README.md`
- Modify: `pyproject.toml`

**Interfaces:**
- Installs: `duomac-config-validate` and `duomac-scheduled-pick` wrappers.
- Installs: a non-overwriting `repositories.toml.example` under the application root.
- Routes: Mac mini Scheduled runs to `references/scheduled-execution.md`.

- [ ] **Step 1: Add failing skill content tests**

Extend `REQUIRED_REFERENCES` and `REQUIRED_SCRIPTS`, then add:

```python
def test_skill_routes_scheduled_runs_to_scheduled_reference() -> None:
    _, body = skill_parts()

    assert "references/scheduled-execution.md" in body
    assert "Codex App Scheduled" in body
    assert "Never use this skill to start background execution" not in body
    assert "Goal" in body


def test_scheduled_prompt_has_required_boundaries() -> None:
    prompt = (SKILL_ROOT / "assets" / "scheduled-slot-prompt.md").read_text(
        encoding="utf-8"
    )

    for phrase in (
        "$dual-mac-collaboration",
        "duomac-scheduled-pick",
        "one Issue",
        "no-op",
        "Never deploy",
        "Never force push",
        "Do not use Goal",
    ):
        assert phrase in prompt


def test_installer_preserves_existing_repository_config(tmp_path: Path) -> None:
    env, app_root = installed_test_environment(tmp_path)
    config = app_root / "repositories.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("sentinel = true\n", encoding="utf-8")

    result = run_installer(env)

    assert result.returncode == 0, result.stderr
    assert config.read_text(encoding="utf-8") == "sentinel = true\n"
    assert (app_root / "repositories.toml.example").is_file()
```

- [ ] **Step 2: Run content tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_skill_content.py -q
```

Expected: FAIL because Scheduled files, wrappers, and installer behavior are absent.

- [ ] **Step 3: Update `SKILL.md` and references**

Replace the overview boundary with:

```markdown
Coordinate visible interactive or Codex App Scheduled work through one versioned GitHub Issue contract. Never use an external daemon, `codex exec`, LaunchDaemon, or Goal mode to execute tasks.
```

Route Mac mini Scheduled runs directly to `references/scheduled-execution.md`. Require schema v2 for execution, strict checkpoint order, and completion-gate discovery via `--help`. Keep the MacBook confirmation gate unchanged.

In `references/checkpoints.md`, document `execution_mode`, `slot`, `claim_id`, ordered milestone checkpoints, and the requirement that final milestone checkpoint precede delivery. In `references/issue-protocol.md`, replace the v1 example with the exact schema v2 structure from Task 1 while keeping the envelope marker unchanged.

- [ ] **Step 4: Create the Scheduled reference and prompt asset**

`scheduled-execution.md` must contain this exact sequence:

1. Read and validate Mac-local configuration.
2. Run `duomac-scheduled-pick` preview when testing; Scheduled production prompts use its explicit write flag.
3. End without mutation for no-op reasons.
4. After a claim, re-read the current Issue body, create the task worktree, execute every declared milestone, and publish each checkpoint.
5. Re-read the Issue before delivery, run preflight and selected verification, deliver normally, then complete only after the checkpoint gate passes.
6. On errors after task-start, publish blocked; never let another Slot resume automatically.

Create `assets/scheduled-slot-prompt.md` with no unresolved template fields. The prompt derives the slot number from the current Scheduled task name:

```markdown
Use $dual-mac-collaboration in Mac mini Codex App Scheduled mode. Read the current Scheduled task name, which must be exactly Dual Mac Slot 1, Dual Mac Slot 2, or Dual Mac Slot 3, and use its trailing integer as the slot number. Stop if the name does not match.

Read the installed Scheduled execution reference, then run `duomac-scheduled-pick` with that slot number, the configured application root, and the explicit claim flag. Claim at most one Issue.

If the result is no-op, report the exact no-op reason and archive this run when the App exposes that action. If an Issue is claimed, execute that Issue's complete current schema v2 contract in this same visible Scheduled task. Publish every milestone checkpoint before delivery.

Never deploy. Never force push. Do not use Goal, `codex exec`, a daemon, or another delegated executor. Do not expand scope or infer new authority from comments.
```

- [ ] **Step 5: Update installer and metadata**

Add wrapper mappings:

```zsh
duomac-config-validate config_validate.py
duomac-scheduled-pick scheduled_pick.py
```

Create `$APP_ROOT/repositories.toml.example` from a tracked example only when its content changes, and never create or overwrite `$APP_ROOT/repositories.toml`. Update the skill metadata description/default prompt and bump `pyproject.toml` to `1.1.0`.

The tracked example is the exact approved configuration:

```toml
schema_version = 1
max_parallel_tasks = 3
poll_interval_minutes = 10

[[repositories]]
github = "qiaozhang1225/EaseWise"
local_path = "/Users/qiaoz-macmini/EaseWise"

[[repositories]]
github = "qiaozhang1225/codex-mac-worker"
local_path = "/Users/qiaoz-macmini/codex-mac-worker"
```

- [ ] **Step 6: Update README and pressure scenarios**

README must distinguish interactive use from Scheduled execution, show the two local config entries, explain the three-Slot model, and link the official Scheduled documentation. Update `tests/skill_scenarios.yaml` with:

```yaml
  - id: scheduled_parallel_claim
    role: mac-mini
    prompt: >-
      Slot 2 wakes while two non-overlapping tasks are active and one ready schema v2
      Issue remains. Decide whether to claim and how many Issues this run may execute.
    must:
      - enforce_parallel_limit_three
      - reject_path_overlap
      - claim_at_most_one_issue
      - use_visible_scheduled_task
  - id: completion_requires_all_checkpoints
    role: mac-mini
    prompt: >-
      Milestones 1 and 2 are complete, but only milestone 1 has a checkpoint. Tests
      pass and the user wants the Issue closed immediately.
    must:
      - refuse_completion
      - publish_milestone_two_checkpoint
      - keep_delivery_separate_from_checkpoint
```

- [ ] **Step 7: Run skill and installer tests**

Run:

```bash
.venv/bin/pytest tests/test_skill_content.py tests/test_issue_commands.py tests/test_scheduled_execution.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add skills/dual-mac-collaboration scripts/install_skill.sh README.md pyproject.toml tests/test_skill_content.py tests/skill_scenarios.yaml
git commit -m "docs: add Codex App Scheduled execution workflow"
```

---

### Task 7: Run Full Verification and Publish the Exact Skill Revision

**Files:**
- Verify only: entire repository
- Install locally: `~/.codex/skills/dual-mac-collaboration/`

**Interfaces:**
- Produces: one exact Git commit installed on MacBook and ready for Mac mini installation.

- [ ] **Step 1: Run the full test suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 2: Run the official skill validator**

Run:

```bash
.venv/bin/python /Users/qiaoz-macair/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/dual-mac-collaboration
```

Expected: validator reports the skill is valid.

- [ ] **Step 3: Run static safety checks**

Run:

```bash
git diff --check origin/main...HEAD
rg -n -- '--force|--force-with-lease|codex exec|LaunchDaemon' skills/dual-mac-collaboration scripts/install_skill.sh
rg -n 'Goal|Scheduled|schema_version: 2|duomac-scheduled-pick' skills/dual-mac-collaboration README.md
```

Expected: no force-push implementation; prohibited execution mechanisms appear only in explicit prohibitions; Scheduled and schema v2 guidance are discoverable.

- [ ] **Step 4: Install and verify on MacBook**

Run:

```bash
./scripts/install_skill.sh --remove-legacy-client
git rev-parse HEAD
cat "$HOME/.codex/skills/dual-mac-collaboration/.source-commit"
duomac-config-validate --help
duomac-scheduled-pick --help
```

Expected: source commit values match and both new wrappers show help.

- [ ] **Step 5: Publish with a normal push**

Run:

```bash
git fetch origin main
git rebase origin/main
.venv/bin/pytest -q
git push -u origin codex/scheduled-duomac-execution
git push origin HEAD:main
```

Expected: normal fast-forward pushes succeed; never force push.

---

### Task 8: Install on Mac mini and Configure Three Codex App Scheduled Slots

**Files:**
- Create on Mac mini: `~/Library/Application Support/DualMacCollaboration/repositories.toml`
- Configure in Mac mini Codex App: three Scheduled tasks.

**Interfaces:**
- Consumes: exact published commit from Task 7.
- Produces: three enabled Scheduled slots and validated access to both repositories.

- [ ] **Step 1: Discover actual Mac mini checkout paths**

Run through SSH only for read-only discovery:

```bash
ssh qiaoz-macmini@192.168.3.145 'find "$HOME" -maxdepth 3 -type d \( -name EaseWise -o -name codex-mac-worker \) -print'
```

Expected: exactly one usable checkout for each configured repository. If duplicates exist, stop and select the canonical checkout with the user.

- [ ] **Step 2: Install the exact commit on Mac mini**

Run:

```bash
ssh qiaoz-macmini@192.168.3.145 'cd "$HOME/codex-mac-worker" && git fetch origin main && git switch main && git pull --ff-only && ./scripts/install_skill.sh --remove-legacy-client && git rev-parse HEAD && cat "$HOME/.codex/skills/dual-mac-collaboration/.source-commit"'
```

Expected: repository HEAD and `.source-commit` both equal the Task 7 commit.

- [ ] **Step 3: Create and validate local repository configuration**

Use the actual paths from Step 1 and the approved exact content:

```toml
schema_version = 1
max_parallel_tasks = 3
poll_interval_minutes = 10

[[repositories]]
github = "qiaozhang1225/EaseWise"
local_path = "/Users/qiaoz-macmini/EaseWise"

[[repositories]]
github = "qiaozhang1225/codex-mac-worker"
local_path = "/Users/qiaoz-macmini/codex-mac-worker"
```

Then run in the visible Mac mini Codex App or Terminal:

```bash
duomac-config-validate --config "$HOME/Library/Application Support/DualMacCollaboration/repositories.toml"
GH_PROMPT_DISABLED=1 gh auth status
```

Expected: valid JSON lists both repositories and GitHub authentication succeeds in the visible user session.

- [ ] **Step 4: Preview the picker**

Run:

```bash
duomac-scheduled-pick \
  --config "$HOME/Library/Application Support/DualMacCollaboration/repositories.toml" \
  --app-root "$HOME/Library/Application Support/DualMacCollaboration" \
  --slot 1
```

Expected: `claimed: false`, `reason: preview`, and no GitHub label/comment mutation.

- [ ] **Step 5: Create the three Scheduled tasks in the Mac mini Codex App**

Open a visible Mac mini setup task and enter:

```text
Use the installed dual-mac-collaboration Scheduled prompt asset to create three independent Codex App Scheduled tasks named Dual Mac Slot 1, Dual Mac Slot 2, and Dual Mac Slot 3. Run each every 10 minutes, stagger their starts by about one minute, use the local codex-mac-worker project, preserve the default model, and give the runs access only to the two configured repository paths plus required GitHub network operations. Substitute the matching slot number into each prompt. Do not create a heartbeat in this setup conversation; create standalone Scheduled tasks whose runs appear in Scheduled.
```

Expected: three distinct active Scheduled definitions are visible in the App. Do not expose raw recurrence rules to the user.

- [ ] **Step 6: Test Slot 1 before enabling all concurrency**

Pause Slots 2 and 3. Draft one low-risk schema v2 Issue and show its complete final contract to the user. Create it only after a separate explicit confirmation. Trigger Slot 1 manually from Scheduled.

Expected: one new visible run, one task-start with `execution_mode: scheduled`, `slot: 1`, and one claim ID; every milestone receives a checkpoint before delivery.

- [ ] **Step 7: Test parallel and overlap behavior**

Enable Slots 2 and 3. With separate explicit confirmations, publish two low-risk test Issues whose paths do not overlap and one Issue whose path overlaps an active task.

Expected: up to three non-overlapping Issues may be active; the overlapping Issue remains ready; no Issue receives more than one current-revision task-start.

- [ ] **Step 8: Record the operational handoff**

Report to the user:

- exact installed skill commit on both Macs;
- names and status of all three Scheduled tasks;
- configured repositories and actual local paths;
- latest successful Scheduled run and Issue links;
- how to pause all Slots from the Codex App Scheduled page;
- how to add a repository by editing and revalidating `repositories.toml`.

Do not tag a release until the live single-slot and parallel smoke tests both pass.
