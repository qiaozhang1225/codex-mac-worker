# Repository Onboarding and Assisted Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an idempotent `codexctl` workflow that onboards App-authorized repositories, verifies Mac mini readiness, reviews Worker PRs, and performs a squash merge only after the user explicitly approves one immutable PR head.

**Architecture:** Keep GitHub transport in `github.py`, put repository lifecycle logic in a new `repository_onboarding.py`, and put immutable PR review/merge gates in a new `assisted_merge.py`. The Mac mini discovers only repositories exposed by its GitHub App installation and containing a valid default-branch project configuration; a probe Issue proves access without running Codex. MacBook write operations use the user's `gh` token and a small local SQLite operation ledger, while the Worker App remains unable to update the default branch under the required Ruleset.

**Tech Stack:** Python 3.12, `argparse`, `httpx`, PyYAML, SQLite, GitHub REST/GraphQL APIs, Git CLI, pytest, macOS launchd.

## Global Constraints

- Do not use Codex Goal/“目标” mode anywhere in code, documentation, tests, or rollout.
- The Worker never calls the merge API and never receives the MacBook user's GitHub credential.
- A merge authorization applies to one repository, one PR, one task hash, and one 40-character head SHA.
- `--expected-head` is mandatory for `repo finalize` and `task merge`; remote drift invalidates approval.
- Onboarding PRs may modify only `.codex-worker/project.toml`, `.github/ISSUE_TEMPLATE/codex-task.yml`, and `.github/workflows/codex-worker-watchdog.yml`.
- Worker discovery accepts only repositories returned by the configured GitHub App installation and having a valid schema-version-1 project configuration on the default branch.
- Normal task merge gates reject high risk, protected/out-of-scope paths, excessive file/line counts, failed or pending checks, conflicts, unresolved review threads, and stale task metadata.
- The initial onboarding bootstrap exception applies only before the repository Ruleset exists; normal Worker PRs require the protected flow.
- All GitHub writes are idempotent and recorded with stable operation IDs; uncertain merge responses are reconciled before retry.
- Continue supporting explicit static `[[repositories]]` entries while adding installation discovery, so the existing Mac mini configuration upgrades without a flag day.
- Python remains `>=3.12`; do not add a new runtime dependency.

---

## File Map

### New production files

- `src/codex_mac_worker/references.py`: parse and format GitHub Issue/PR references.
- `src/codex_mac_worker/control_state.py`: MacBook-only SQLite ledger for idempotent write operations.
- `src/codex_mac_worker/repository_onboarding.py`: standard assets, onboarding snapshots, Ruleset/label reconciliation, probes, readiness.
- `src/codex_mac_worker/assisted_merge.py`: delivery metadata, review snapshots, approval fingerprints, merge gates, audit comments.
- `src/codex_mac_worker/assets/codex-task.yml`: packaged fallback Issue form.
- `src/codex_mac_worker/assets/codex-worker-watchdog.yml`: packaged watchdog workflow.

### Existing production files to modify

- `src/codex_mac_worker/github.py`: pagination, repository/file/label/Ruleset/check/review/merge API methods.
- `src/codex_mac_worker/config.py`: optional installation repository discovery.
- `src/codex_mac_worker/protocol.py`: repository probe and Worker delivery machine blocks.
- `src/codex_mac_worker/prompting.py`: structured per-criterion acceptance results.
- `src/codex_mac_worker/worker.py`: delivery metadata and probe handling; still no merge call.
- `src/codex_mac_worker/daemon.py`: refresh App-installed repositories and route probe Issues.
- `src/codex_mac_worker/cli.py`: `repo onboard/status/finalize` and `task review/merge` commands.
- `templates/worker.toml.example`: enable safe installation discovery by default.
- `pyproject.toml`: package the two asset files.
- `skills/dispatch-codex-task/SKILL.md`: add repository lifecycle and explicit per-PR approval protocol.
- `docs/MACBOOK_SETUP.md`, `docs/MAC_MINI_SETUP.md`, `docs/OPERATIONS.md`, `docs/SECURITY.md`: installation, operation, and trust-boundary updates.
- `scripts/bootstrap_repository.sh`: deprecate manual label creation in favor of `codexctl repo onboard` while preserving a compatibility wrapper.

### Tests

- `tests/test_references.py`
- `tests/test_control_state.py`
- `tests/test_repository_onboarding.py`
- `tests/test_assisted_merge.py`
- `tests/test_worker_discovery.py`
- Modify `tests/test_cli.py`, `tests/test_github.py`, `tests/test_protocol.py`, `tests/test_prompting_verification.py`, `tests/test_worker_service.py`, `tests/test_daemon.py`, `tests/test_worker_config.py`, `tests/test_dispatch_skill.py`, and `tests/test_operational_assets.py`.

---

### Task 1: GitHub references and MacBook operation ledger

**Files:**
- Create: `src/codex_mac_worker/references.py`
- Create: `src/codex_mac_worker/control_state.py`
- Create: `tests/test_references.py`
- Create: `tests/test_control_state.py`
- Modify: `src/codex_mac_worker/control.py:64-70`

**Interfaces:**
- Produces: `IssueReference(repo: str, number: int)`, `PullRequestReference(repo: str, number: int)`, `parse_issue_reference(str)`, and `parse_pull_request_reference(str)`.
- Produces: `ControlState(path: Path)`, `operation_id(action, target, expected_head)`, `begin(...)`, `complete(...)`, and `get(...)`.
- Consumes: Python standard library only.

- [ ] **Step 1: Write failing reference parser tests**

```python
from codex_mac_worker.references import parse_issue_reference, parse_pull_request_reference


def test_references_accept_urls_and_short_forms() -> None:
    assert parse_issue_reference("https://github.com/owner/repo/issues/12").repo == "owner/repo"
    assert parse_issue_reference("owner/repo#12").number == 12
    assert parse_pull_request_reference("https://github.com/owner/repo/pull/44").number == 44
    assert parse_pull_request_reference("owner/repo#44").repo == "owner/repo"
```

- [ ] **Step 2: Run the reference test and verify the missing-module failure**

Run: `.venv/bin/pytest tests/test_references.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: codex_mac_worker.references`.

- [ ] **Step 3: Implement immutable reference types and delegate the old parser**

```python
# src/codex_mac_worker/references.py
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class IssueReference:
    repo: str
    number: int


@dataclass(frozen=True, slots=True)
class PullRequestReference:
    repo: str
    number: int


def _parse(reference: str, resource: str) -> tuple[str, int]:
    url = re.fullmatch(rf"https://github\.com/([^/]+/[^/]+)/{resource}/(\d+)/?", reference)
    short = re.fullmatch(r"([^/]+/[^#]+)#(\d+)", reference)
    match = url or short
    if not match:
        raise ValueError(f"reference must be a GitHub {resource} URL or owner/repo#number")
    return match.group(1), int(match.group(2))


def parse_issue_reference(reference: str) -> IssueReference:
    repo, number = _parse(reference, "issues")
    return IssueReference(repo, number)


def parse_pull_request_reference(reference: str) -> PullRequestReference:
    repo, number = _parse(reference, "pull")
    return PullRequestReference(repo, number)
```

Keep `control.parse_issue_reference()` as a compatibility wrapper returning `(repo, number)` so current callers and tests do not break.

- [ ] **Step 4: Write failing idempotency-ledger tests**

```python
from pathlib import Path

from codex_mac_worker.control_state import ControlState, operation_id


def test_operation_ledger_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "codexctl.sqlite3"
    key = operation_id("task-merge", "owner/repo#44", "a" * 40)
    state = ControlState(path)
    assert state.begin(key, "task-merge", "owner/repo#44", "a" * 40) is True
    assert state.begin(key, "task-merge", "owner/repo#44", "a" * 40) is False
    state.complete(key, {"merged": True, "sha": "b" * 40})
    state.close()

    reopened = ControlState(path)
    assert reopened.get(key)["result"] == {"merged": True, "sha": "b" * 40}
```

- [ ] **Step 5: Run the ledger test and verify the missing-module failure**

Run: `.venv/bin/pytest tests/test_control_state.py -q`

Expected: FAIL during collection with `ModuleNotFoundError: codex_mac_worker.control_state`.

- [ ] **Step 6: Implement the SQLite operation ledger**

```python
# src/codex_mac_worker/control_state.py
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


def operation_id(action: str, target: str, expected_head: str) -> str:
    raw = f"v1\0{action}\0{target}\0{expected_head}".encode()
    return hashlib.sha256(raw).hexdigest()


class ControlState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                expected_head TEXT NOT NULL,
                state TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT
            )"""
        )
        self.connection.commit()

    def begin(self, key: str, action: str, target: str, expected_head: str) -> bool:
        created = datetime.now(UTC).isoformat()
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO operations VALUES (?, ?, ?, ?, 'started', NULL, ?, NULL)",
            (key, action, target, expected_head, created),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def complete(self, key: str, result: dict[str, Any]) -> None:
        self.connection.execute(
            "UPDATE operations SET state='completed', result_json=?, completed_at=? WHERE operation_id=?",
            (json.dumps(result, sort_keys=True), datetime.now(UTC).isoformat(), key),
        )
        self.connection.commit()

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM operations WHERE operation_id=?", (key,)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["result"] = json.loads(item.pop("result_json")) if item["result_json"] else None
        return item

    def close(self) -> None:
        self.connection.close()
```

- [ ] **Step 7: Run focused tests**

Run: `.venv/bin/pytest tests/test_references.py tests/test_control_state.py tests/test_commands_cli.py -q`

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add src/codex_mac_worker/references.py src/codex_mac_worker/control_state.py src/codex_mac_worker/control.py tests/test_references.py tests/test_control_state.py
git commit -m "feat: add immutable GitHub references and ctl ledger"
```

---

### Task 2: Complete the GitHub API surface with pagination and review threads

**Files:**
- Modify: `src/codex_mac_worker/github.py:80-206`
- Modify: `tests/test_github.py:80-end`

**Interfaces:**
- Produces: `get_repository`, `get_authenticated_user`, `get_repository_file`, `list_installation_repositories`, `list_labels`, `upsert_label`, `list_pull_files`, `list_check_runs`, `get_combined_status`, `list_reviews`, `list_review_threads`, `list_rulesets`, `create_ruleset`, `update_ruleset`, `update_pull_request`, `create_pull_review`, and `merge_pull_request`.
- Produces: `_paginate(path, *, params, list_key=None)` with stable multi-page behavior.
- Consumes: existing `GitHubClient._request` and `GitHubError`.

- [ ] **Step 1: Write failing REST pagination and merge-request tests**

```python
def test_client_paginates_files_and_sends_expected_sha_on_merge() -> None:
    seen: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, payload))
        if request.url.path.endswith("/files"):
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=[{"filename": f"src/{page}.py"}] if page < 3 else [])
        if request.url.path.endswith("/merge"):
            return httpx.Response(200, json={"merged": True, "sha": "b" * 40})
        raise AssertionError(request.url)

    client = GitHubClient(token_provider=lambda: "token", transport=httpx.MockTransport(handler))
    assert [item["filename"] for item in client.list_pull_files("owner/repo", 44)] == [
        "src/1.py", "src/2.py"
    ]
    result = client.merge_pull_request("owner/repo", 44, expected_head="a" * 40)
    assert result["merged"] is True
    assert seen[-1][2] == {"merge_method": "squash", "sha": "a" * 40}
```

- [ ] **Step 2: Write a failing GraphQL unresolved-thread test**

```python
def test_client_reads_review_thread_resolution() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/graphql"
        return httpx.Response(200, json={"data": {"repository": {"pullRequest": {
            "reviewThreads": {"nodes": [
                {"isResolved": False, "comments": {"nodes": [{"url": "https://example/thread"}]}}
            ]}
        }}}})

    client = GitHubClient(token_provider=lambda: "token", transport=httpx.MockTransport(handler))
    assert client.list_review_threads("owner/repo", 44)[0]["isResolved"] is False
```

- [ ] **Step 3: Run the focused tests and verify missing methods**

Run: `.venv/bin/pytest tests/test_github.py -q`

Expected: FAIL with `AttributeError` for `list_pull_files` or `list_review_threads`.

- [ ] **Step 4: Add pagination, repository, label, Ruleset, check, review, and merge methods**

Implement pagination until an empty page or a page shorter than 100 items. Use these exact endpoint mappings:

```python
def list_pull_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
    return self._paginate(f"/repos/{repo}/pulls/{pr_number}/files")

def list_check_runs(self, repo: str, sha: str) -> list[dict[str, Any]]:
    return self._paginate(f"/repos/{repo}/commits/{sha}/check-runs", list_key="check_runs")

def get_combined_status(self, repo: str, sha: str) -> dict[str, Any]:
    return self._request("GET", f"/repos/{repo}/commits/{sha}/status")

def merge_pull_request(self, repo: str, pr_number: int, *, expected_head: str) -> dict[str, Any]:
    return self._request(
        "PUT", f"/repos/{repo}/pulls/{pr_number}/merge",
        json={"merge_method": "squash", "sha": expected_head},
    )
```

For unresolved threads, call `/graphql` with owner, repository name, PR number, and cursor; paginate `reviewThreads.pageInfo` and return flattened nodes. Raise `GitHubError(retryable=False)` if the GraphQL response contains `errors`.

The repository-file method must request a specific `ref`, base64-decode the `content` field, and return UTF-8 text. The Ruleset methods must use `/repos/{repo}/rulesets`. `upsert_label` must PATCH an existing label or POST a missing label after a 404-only lookup; do not treat other errors as absence.

- [ ] **Step 5: Run all GitHub client tests**

Run: `.venv/bin/pytest tests/test_github.py tests/test_durable_github.py -q`

Expected: all tests PASS; durable Worker operations remain unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/codex_mac_worker/github.py tests/test_github.py
git commit -m "feat: add GitHub onboarding and review APIs"
```

---

### Task 3: Discover App-installed repositories and prove readiness without running Codex

**Files:**
- Modify: `src/codex_mac_worker/config.py:26-40,124-163`
- Modify: `src/codex_mac_worker/protocol.py:10-170`
- Modify: `src/codex_mac_worker/daemon.py:42-89,266-290`
- Modify: `src/codex_mac_worker/worker.py:27-37,77-148`
- Modify: `templates/worker.toml.example`
- Create: `tests/test_worker_discovery.py`
- Modify: `tests/test_worker_config.py`, `tests/test_protocol.py`, `tests/test_daemon.py`, `tests/test_worker_service.py`

**Interfaces:**
- Produces: `WorkerConfig.discover_installation_repositories: bool`.
- Produces: `REPOSITORY_PROBE_MARKER`, `RepositoryProbe`, `parse_repository_probe`, `render_repository_probe`, and `render_repository_attestation`.
- Produces: `WorkerDaemon.repositories()` and `WorkerService.process_repository_probe(...)`.
- Consumes: `GitHubClient.list_installation_repositories()` and `get_repository_file()` from Task 2.

- [ ] **Step 1: Write failing configuration tests for discovery and static compatibility**

```python
def test_worker_config_allows_installation_discovery_without_static_repositories(tmp_path: Path) -> None:
    path = write_worker_config(tmp_path, repositories="", extra="discover_installation_repositories = true")
    config = load_worker_config(path)
    assert config.discover_installation_repositories is True
    assert config.repositories == ()


def test_worker_config_requires_one_repository_source(tmp_path: Path) -> None:
    path = write_worker_config(tmp_path, repositories="", extra="")
    with pytest.raises(ConfigError, match="repository source"):
        load_worker_config(path)
```

- [ ] **Step 2: Implement the discovery configuration**

Add `discover_installation_repositories: bool` to `WorkerConfig`. Parse it as a strict boolean defaulting to `False`; allow an empty `repositories` array only when it is `True`. Set this in the example:

```toml
discover_installation_repositories = true
```

Keep the current EaseWise `[[repositories]]` entry during rollout; discovery deduplicates by full repository name.

- [ ] **Step 3: Write failing probe protocol tests**

```python
def test_repository_probe_round_trip_binds_default_head_and_config_hash() -> None:
    body = render_repository_probe(
        probe_id="probe-1", default_head="a" * 40, project_config_hash="b" * 64
    )
    probe = parse_repository_probe(body)
    assert probe.probe_id == "probe-1"
    assert probe.default_head == "a" * 40
    assert probe.project_config_hash == "b" * 64
```

- [ ] **Step 4: Implement probe and attestation machine blocks**

Use these markers and immutable fields:

```python
REPOSITORY_PROBE_MARKER = "<!-- codex-repository-probe:v1 -->"
REPOSITORY_ATTESTATION_MARKER = "<!-- codex-worker-readiness:v1 -->"

@dataclass(frozen=True, slots=True)
class RepositoryProbe:
    probe_id: str
    default_head: str
    project_config_hash: str
```

Both renderers use one fenced YAML block with `schema_version: 1`. Parsers reject duplicate blocks, non-hex SHA/hash values, missing IDs, and unknown schema versions.

- [ ] **Step 5: Write failing repository discovery and probe routing tests**

```python
def test_daemon_discovers_only_installed_repositories_with_valid_project_config() -> None:
    github = DiscoveryGitHub(
        installed=[
            {"full_name": "owner/ready", "clone_url": "https://github.com/owner/ready.git", "default_branch": "main"},
            {"full_name": "owner/missing", "clone_url": "https://github.com/owner/missing.git", "default_branch": "main"},
        ],
        files={"owner/ready": VALID_PROJECT_TOML},
    )
    daemon = make_daemon(github, discover=True)
    assert [repo.name for repo in daemon.repositories()] == ["owner/ready"]


def test_probe_is_attested_without_invoking_runner() -> None:
    service, github, runner = make_service_for_probe()
    service.process_repository_probe(REPOSITORY, PROBE_ISSUE)
    assert runner.calls == []
    assert "<!-- codex-worker-readiness:v1 -->" in github.comments[-1]
    assert github.updated_issue["state"] == "closed"
```

- [ ] **Step 6: Implement discovery and probe routing**

`WorkerDaemon.repositories()` must:

1. Start with static configured repositories.
2. If discovery is enabled, list App installation repositories.
3. For each candidate, read `.codex-worker/project.toml` at its reported default branch.
4. Parse it with `load_project_config` using a secure temporary file or a new `parse_project_config(text)` helper.
5. Require `default_base_branch` to equal the GitHub default branch.
6. Deduplicate and sort by `full_name`.
7. Cache the result in `worker_state` for at most five minutes; on GitHub failure use the last persisted list but do not add a new repository.

In `run_once()`, detect the repository-probe marker before normal task parsing. `process_repository_probe()` first applies the same authorized-user and collaborator-permission checks as a normal task, then verifies the current default-branch SHA and SHA-256 of the current project config against the Issue block. It posts the attestation with Worker ID and timestamp, replaces the status label with `codex:completed`, and closes the Issue. It never creates a worktree or calls the runner. The GitHub comment author's API login is the authoritative Worker App identity; `repo status` exposes it as `worker_login` for later PR-author checks.

- [ ] **Step 7: Run discovery, protocol, daemon, and service tests**

Run: `.venv/bin/pytest tests/test_worker_discovery.py tests/test_worker_config.py tests/test_protocol.py tests/test_daemon.py tests/test_worker_service.py -q`

Expected: all tests PASS.

- [ ] **Step 8: Commit**

```bash
git add src/codex_mac_worker/config.py src/codex_mac_worker/protocol.py src/codex_mac_worker/daemon.py src/codex_mac_worker/worker.py templates/worker.toml.example tests/test_worker_discovery.py tests/test_worker_config.py tests/test_protocol.py tests/test_daemon.py tests/test_worker_service.py
git commit -m "feat: discover and attest App-authorized repositories"
```

---

### Task 4: Package onboarding assets and validate exact onboarding scope

**Files:**
- Create: `src/codex_mac_worker/assets/codex-task.yml`
- Create: `src/codex_mac_worker/assets/codex-worker-watchdog.yml`
- Create: `src/codex_mac_worker/repository_onboarding.py`
- Create: `tests/test_repository_onboarding.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `ONBOARDING_PATHS`, `STATUS_LABELS`, `OnboardingSnapshot`, `inspect_onboarding_pr(...)`, `render_project_config(...)`, and `load_asset(name)`.
- Consumes: `load_project_config`, `GitHubClient`, and `PullRequestReference`.

- [ ] **Step 1: Write failing exact-scope and project-config tests**

```python
def test_onboarding_snapshot_accepts_only_three_standard_files() -> None:
    github = OnboardingGitHub(files=STANDARD_FILES, head="a" * 40, base="main")
    snapshot = inspect_onboarding_pr(github, "owner/repo", 1)
    assert snapshot.changed_paths == tuple(sorted(ONBOARDING_PATHS))
    assert snapshot.head_sha == "a" * 40


def test_onboarding_snapshot_rejects_a_fourth_file() -> None:
    github = OnboardingGitHub(files=[*STANDARD_FILES, {"filename": "README.md"}], head="a" * 40)
    with pytest.raises(OnboardingError, match="exactly the three standard files"):
        inspect_onboarding_pr(github, "owner/repo", 1)
```

- [ ] **Step 2: Add packaged assets using the already reviewed EaseWise contents**

Copy the exact Issue form and watchdog workflow currently present in EaseWise PR #1 into the two asset files. Add package data:

```toml
[tool.setuptools.package-data]
codex_mac_worker = ["assets/*.yml"]
```

Load them with `importlib.resources.files("codex_mac_worker").joinpath("assets", name).read_text()`.

- [ ] **Step 3: Implement strict onboarding inspection**

Use these immutable types and constants:

```python
ONBOARDING_PATHS = frozenset({
    ".codex-worker/project.toml",
    ".github/ISSUE_TEMPLATE/codex-task.yml",
    ".github/workflows/codex-worker-watchdog.yml",
})

@dataclass(frozen=True, slots=True)
class OnboardingSnapshot:
    repo: str
    pr_number: int
    url: str
    base_branch: str
    base_sha: str
    head_sha: str
    changed_paths: tuple[str, ...]
    project_config_hash: str
    is_draft: bool
    mergeable: bool
```

`inspect_onboarding_pr()` fetches the PR and all changed files, requires the exact path set, rejects deleted/renamed files, reads each file from the PR head, validates `project.toml`, requires its default branch to match the PR base, and hashes the raw config bytes. The function is read-only.

- [ ] **Step 4: Add deterministic project-config rendering for new repositories**

Expose:

```python
def render_project_config(*, default_branch: str, fast_commands: tuple[str, ...], full_commands: tuple[str, ...]) -> str:
    if not fast_commands:
        raise OnboardingError("at least one repository-approved fast verification command is required")
```

Render schema version 1, risks `low` and `medium`, protected paths `.codex`, `.codex-worker`, `.github/workflows`, `.env`, `.env.local`, and `product/deploy`, limits 30 files/3000 lines/45 minutes/120 minutes/two attempts, and the supplied commands. Never infer commands from package-manager files.

- [ ] **Step 5: Run packaging and onboarding tests**

Run: `.venv/bin/pip install -e . && .venv/bin/pytest tests/test_repository_onboarding.py tests/test_operational_assets.py -q`

Expected: all tests PASS and `load_asset()` works from the editable install.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/codex_mac_worker/assets src/codex_mac_worker/repository_onboarding.py tests/test_repository_onboarding.py tests/test_operational_assets.py
git commit -m "feat: add strict repository onboarding assets"
```

---

### Task 5: Implement `repo onboard`, `repo status`, and approval-bound `repo finalize`

**Files:**
- Modify: `src/codex_mac_worker/repository_onboarding.py`
- Modify: `src/codex_mac_worker/cli.py:51-106`
- Modify: `tests/test_repository_onboarding.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Produces: `prepare_onboarding(...) -> OnboardingSnapshot`, `repository_status(...) -> ReadinessReport`, and `finalize_onboarding(...) -> ReadinessReport`.
- Produces CLI: `codexctl repo onboard --repo OWNER/REPO [--adopt-pr N] [--project-config PATH]`, `codexctl repo status OWNER/REPO`, and `codexctl repo finalize PR_URL --expected-head SHA`.
- Consumes: Tasks 1-4 and MacBook personal GitHub token.

- [ ] **Step 1: Write failing CLI parser tests**

```python
def test_ctl_parser_supports_repository_lifecycle() -> None:
    parser = build_ctl_parser()
    onboard = parser.parse_args(["repo", "onboard", "--repo", "owner/repo", "--adopt-pr", "1"])
    status = parser.parse_args(["repo", "status", "owner/repo"])
    finalize = parser.parse_args([
        "repo", "finalize", "https://github.com/owner/repo/pull/1",
        "--expected-head", "a" * 40,
    ])
    assert (onboard.resource, onboard.action, onboard.adopt_pr) == ("repo", "onboard", 1)
    assert status.action == "status"
    assert finalize.expected_head == "a" * 40
```

- [ ] **Step 2: Write failing finalize drift and idempotency tests**

```python
def test_finalize_rejects_head_drift_before_any_write(tmp_path: Path) -> None:
    github = OnboardingGitHub(head="b" * 40)
    with pytest.raises(OnboardingError, match="approval expired"):
        finalize_onboarding(
            github, ControlState(tmp_path / "state.db"),
            PullRequestReference("owner/repo", 1), expected_head="a" * 40,
        )
    assert github.writes == []


def test_finalize_reconciles_an_already_merged_pr(tmp_path: Path) -> None:
    github = OnboardingGitHub(head="a" * 40, merged=True, ready_repository=True)
    report = finalize_onboarding(
        github, ControlState(tmp_path / "state.db"),
        PullRequestReference("owner/repo", 1), expected_head="a" * 40,
    )
    assert report.phase in {"awaiting-worker", "ready"}
    assert github.merge_calls == 0
```

- [ ] **Step 3: Implement onboarding PR creation and adoption**

For `--adopt-pr`, call `inspect_onboarding_pr()` and print JSON containing URL, base/head SHA, exact paths, config hash, Draft state, and mergeability.

For a new PR, require `--project-config PATH`; validate the file before writing. Clone into `TemporaryDirectory`, create/reset only `codex/onboard-worker` from `origin/<default>`, copy the validated config plus packaged assets, verify `git diff --name-only` equals `ONBOARDING_PATHS`, commit, push with a temporary `GIT_ASKPASS`, and create or reuse one Draft PR. Never put the token in a remote URL or command argument.

- [ ] **Step 4: Implement standard labels and the Ruleset payload**

Define all nine labels with fixed colors/descriptions. Build one Ruleset named `Codex Worker Default Branch` with target `branch`, include `~DEFAULT_BRANCH`, enforcement `active`, and rules `deletion`, `non_fast_forward`, `update`, and `pull_request`. Configure the repository Admin role (`actor_type: RepositoryRole`, `actor_id: 5`) as `bypass_mode: pull_request`; this permits the owner to merge a PR but does not permit the write-level Worker App to update the default branch. Pull request parameters require one approval, stale-review dismissal, last-push approval, resolved threads, and squash-only merge.

Before updating an existing same-name Ruleset, compare its normalized security fields. Refuse to overwrite a different Ruleset with the same name if it grants an Integration bypass.

- [ ] **Step 5: Implement finalize ordering and the repository probe**

`finalize_onboarding()` must execute this exact order:

1. Inspect the PR again and compare `head_sha` to `expected_head`.
2. Confirm clean/mergeable status and exact three-file scope.
3. If Draft, mark Ready; onboarding self-review is not attempted.
4. Merge with squash and expected SHA, or reconcile if already merged.
5. Verify the three files on the new default-branch head.
6. Upsert all nine labels.
7. Create or reconcile the protected Ruleset.
8. Create one probe Issue containing current default head and project-config hash, labeled `codex:queued`.
9. Return phase `awaiting-worker` until a matching App-authored readiness attestation exists.

Every write uses a stable `operation_id`. If merge raises a retryable or transport error, query the PR; continue only if `merged_at` is present and its recorded head matches the approved SHA.

- [ ] **Step 6: Implement readiness reporting**

```python
@dataclass(frozen=True, slots=True)
class ReadinessReport:
    repo: str
    phase: str
    default_branch: str
    default_head: str
    files_valid: bool
    labels_valid: bool
    ruleset_valid: bool
    worker_attested: bool
    worker_login: str | None
    blockers: tuple[str, ...]
```

Return `ready` only when the files, labels, Ruleset, and an App-authored attestation matching the current default head/config hash are all valid. Return `blocked` for security drift and `awaiting-worker` when only attestation is missing.

- [ ] **Step 7: Run onboarding and CLI tests**

Run: `.venv/bin/pytest tests/test_repository_onboarding.py tests/test_cli.py -q`

Expected: all tests PASS; no live repository is contacted.

- [ ] **Step 8: Commit**

```bash
git add src/codex_mac_worker/repository_onboarding.py src/codex_mac_worker/cli.py tests/test_repository_onboarding.py tests/test_cli.py
git commit -m "feat: add idempotent repository onboarding commands"
```

---

### Task 6: Publish trustworthy Worker delivery metadata and acceptance evidence

**Files:**
- Modify: `src/codex_mac_worker/prompting.py:7-31`
- Modify: `src/codex_mac_worker/protocol.py`
- Modify: `src/codex_mac_worker/worker.py:190-195,501-556,844-884`
- Modify: `src/codex_mac_worker/github.py`
- Modify: `tests/test_prompting_verification.py`, `tests/test_protocol.py`, `tests/test_worker_service.py`, `tests/test_github.py`

**Interfaces:**
- Produces: required Codex result field `acceptance_results`.
- Produces: `DeliveryMetadata` plus `render_delivery_block()` and `parse_delivery_block()`.
- Consumes: existing `TaskSpec`, `RunnerResult`, and verification results.

- [ ] **Step 1: Write failing schema and delivery round-trip tests**

```python
def test_result_schema_requires_acceptance_results() -> None:
    schema = result_schema()
    assert "acceptance_results" in schema["required"]
    assert schema["properties"]["acceptance_results"]["items"]["properties"]["status"]["enum"] == [
        "met", "not_met", "needs_review"
    ]


def test_delivery_metadata_round_trip_binds_latest_commit() -> None:
    metadata = DeliveryMetadata(
        issue_number=12, task_hash="b" * 64, context_commit="a" * 40,
        delivery_commit="c" * 40, verification_profile="fast",
        verification_passed=True, model="gpt-5", cli_version="codex 1.2.3",
        acceptance_results=({"criterion": "Tests pass", "status": "met", "evidence": "pytest"},),
        risks=(), needs_human=(),
    )
    assert parse_delivery_block(render_delivery_block(metadata)) == metadata
```

- [ ] **Step 2: Extend the structured result contract**

Add this required property:

```python
"acceptance_results": {
    "type": "array",
    "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["criterion", "status", "evidence"],
        "properties": {
            "criterion": {"type": "string", "minLength": 1},
            "status": {"type": "string", "enum": ["met", "not_met", "needs_review"]},
            "evidence": {"type": "string", "minLength": 1},
        },
    },
}
```

In `_require_completed_result`, require exactly one result for every acceptance criterion in original order and reject `not_met`. Preserve `needs_review` for the human summary rather than silently treating it as met.

- [ ] **Step 3: Add versioned Worker delivery metadata**

Use marker `<!-- codex-worker-delivery:v1 -->` and one fenced YAML block. The parser validates full SHA/hash lengths, positive issue number, boolean verification result, and accepted status values. It rejects duplicate blocks.

- [ ] **Step 4: Render and update PR bodies from one helper**

Create `WorkerService._delivery_pr_body(...)` that includes the machine block followed by human-readable acceptance, verification commands/results, risks, and human dependencies. Initial delivery calls `create_draft_pr`; a revision calls `update_pull_request(..., body=new_body)` after pushing the revision commit. Thus the metadata always binds the current PR head and latest verification run.

The Worker GitHub protocol gains only `update_pull_request`; it does not gain `merge_pull_request`.

- [ ] **Step 5: Run delivery-focused tests**

Run: `.venv/bin/pytest tests/test_prompting_verification.py tests/test_protocol.py tests/test_worker_service.py tests/test_github.py -q`

Expected: all tests PASS, including a revision test proving old delivery metadata is replaced by the new commit SHA.

- [ ] **Step 6: Commit**

```bash
git add src/codex_mac_worker/prompting.py src/codex_mac_worker/protocol.py src/codex_mac_worker/worker.py src/codex_mac_worker/github.py tests/test_prompting_verification.py tests/test_protocol.py tests/test_worker_service.py tests/test_github.py
git commit -m "feat: publish immutable Worker delivery evidence"
```

---

### Task 7: Build read-only task review and all merge gates

**Files:**
- Create: `src/codex_mac_worker/assisted_merge.py`
- Create: `tests/test_assisted_merge.py`
- Modify: `src/codex_mac_worker/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Produces: `ReviewSnapshot`, `GateResult`, `review_task(...)`, `evaluate_merge_gates(...)`, and `approval_fingerprint(...)`.
- Produces CLI: `codexctl task review ISSUE_URL`.
- Consumes: `TaskSpec`, `DeliveryMetadata`, `ProjectConfig`, GitHub API methods, and `validate_changed_paths`.

- [ ] **Step 1: Write a failing happy-path review test**

```python
def test_review_snapshot_binds_issue_pr_checks_paths_and_threads() -> None:
    github = ReviewGitHub.happy_path()
    snapshot = review_task(github, IssueReference("owner/repo", 12))
    assert snapshot.pr_number == 44
    assert snapshot.head_sha == "c" * 40
    assert snapshot.task_hash == "b" * 64
    assert snapshot.gates.allowed is True
    assert len(snapshot.approval_fingerprint) == 64
```

- [ ] **Step 2: Write parameterized failing gate tests**

```python
@pytest.mark.parametrize(
    ("mutation", "blocker"),
    [
        ("failed_check", "checks"),
        ("pending_check", "checks"),
        ("unresolved_thread", "review threads"),
        ("conflict", "mergeable"),
        ("outside_path", "allowed_paths"),
        ("protected_path", "protected"),
        ("high_risk", "risk"),
        ("task_hash_drift", "task hash"),
        ("delivery_sha_drift", "delivery commit"),
        ("non_worker_branch", "codex/"),
        ("ruleset_drift", "Ruleset"),
    ],
)
def test_review_blocks_each_unsafe_state(mutation: str, blocker: str) -> None:
    github = ReviewGitHub.with_mutation(mutation)
    snapshot = review_task(github, IssueReference("owner/repo", 12))
    assert snapshot.gates.allowed is False
    assert any(blocker.lower() in item.lower() for item in snapshot.gates.blockers)
```

- [ ] **Step 3: Implement review data types and fingerprint**

```python
@dataclass(frozen=True, slots=True)
class GateResult:
    allowed: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    repo: str
    issue_number: int
    pr_number: int
    pr_url: str
    base_branch: str
    base_sha: str
    head_sha: str
    is_draft: bool
    task_hash: str
    context_commit: str
    changed_paths: tuple[str, ...]
    additions: int
    deletions: int
    checks: tuple[dict[str, str], ...]
    acceptance_results: tuple[dict[str, str], ...]
    model: str | None
    cli_version: str | None
    risks: tuple[str, ...]
    needs_human: tuple[str, ...]
    unresolved_threads: tuple[str, ...]
    gates: GateResult
    approval_fingerprint: str
```

The fingerprint is SHA-256 of canonical JSON containing schema version, repository, Issue, PR, task hash, context commit, base SHA, and head SHA.

- [ ] **Step 4: Implement read-only review assembly**

`review_task()` must:

1. Read and parse the frozen Issue task block.
2. Locate exactly one open PR whose delivery block names the Issue; reject zero or multiple matches.
3. Require `codex/*` head, require the PR author to equal the current readiness attestation's authoritative `worker_login`, and require delivery metadata matching task hash/context/current head.
4. Read project config from the PR base SHA and apply existing path/diff policy to PR file stats.
5. Collect Check Runs and legacy commit statuses. Block queued/in-progress/pending and every action_required/failure/cancelled/timed_out/stale/skipped conclusion; accept success, and accept neutral only for a check not named as required by the Ruleset.
6. Collect GraphQL review threads and block every unresolved thread.
7. Confirm GitHub reports mergeable and the required Ruleset security fields still match.
8. Preserve Codex acceptance evidence, risks, model, CLI version, and human dependencies in the snapshot.

This function performs no GitHub writes and does not change Draft state. A Draft Worker PR may still receive `gates.allowed = True`; the snapshot records `is_draft: true`, and only `task merge` may transition it to Ready after explicit approval.

- [ ] **Step 5: Add `task review` JSON output**

Parse the Issue URL, call `review_task`, and print the complete snapshot as JSON. Return exit code 0 when allowed and 2 when blocked, so the skill can distinguish a review blocker from a transport error.

- [ ] **Step 6: Run review and parser tests**

Run: `.venv/bin/pytest tests/test_assisted_merge.py tests/test_cli.py -q`

Expected: all tests PASS and every parameterized unsafe state produces a named blocker.

- [ ] **Step 7: Commit**

```bash
git add src/codex_mac_worker/assisted_merge.py src/codex_mac_worker/cli.py tests/test_assisted_merge.py tests/test_cli.py
git commit -m "feat: add immutable Worker PR review gates"
```

---

### Task 8: Execute one explicitly approved merge and audit it

**Files:**
- Modify: `src/codex_mac_worker/assisted_merge.py`
- Modify: `src/codex_mac_worker/cli.py`
- Modify: `tests/test_assisted_merge.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Produces: `merge_task(...) -> MergeResult` and `render_approval_audit(...)`.
- Produces CLI: `codexctl task merge ISSUE_URL --expected-head SHA`.
- Consumes: `review_task`, `ControlState`, authenticated user identity, and merge API from Task 2.

- [ ] **Step 1: Write failing stale-approval and successful-merge tests**

```python
def test_merge_rechecks_head_and_writes_nothing_after_drift(tmp_path: Path) -> None:
    github = ReviewGitHub.happy_path(current_head="d" * 40)
    with pytest.raises(MergeBlocked, match="approval expired"):
        merge_task(
            github, ControlState(tmp_path / "state.db"),
            IssueReference("owner/repo", 12), expected_head="c" * 40,
        )
    assert github.writes == []


def test_merge_approves_squashes_and_records_audit(tmp_path: Path) -> None:
    github = ReviewGitHub.happy_path(is_draft=True)
    result = merge_task(
        github, ControlState(tmp_path / "state.db"),
        IssueReference("owner/repo", 12), expected_head="c" * 40,
    )
    assert result.merged is True
    assert github.ready_calls == 1
    assert github.merge_payload == {"merge_method": "squash", "sha": "c" * 40}
    assert "<!-- codex-human-approval:v1 -->" in github.comments[-1]
```

- [ ] **Step 2: Implement final re-review and identity checks**

At command start, rebuild `ReviewSnapshot`; require all gates allowed and exact head equality. Read `/user` and require the login to have `admin` or `maintain` permission. If the snapshot is Draft, mark it Ready, then rebuild the entire snapshot and re-check every gate and the same head SHA. If the PR author differs from the user and no current approval exists from that user, submit an approval review. If the PR author equals the user, stop for normal task PRs; the self-authored exception exists only in `repo finalize`.

- [ ] **Step 3: Implement idempotent squash merge and uncertain-result reconciliation**

Compute operation ID from `task-merge`, `repo#pr`, and expected head. If a completed operation exists, query GitHub and return its result only when the PR is actually merged. If the merge request errors, immediately query the PR: treat it as success only when `merged_at` is set and the pre-merge PR head equaled `expected_head`; otherwise re-raise without a second merge request.

- [ ] **Step 4: Add the structured audit comment**

Render marker `<!-- codex-human-approval:v1 -->` with schema version, approval fingerprint, actor login, Issue number, PR number, task hash, approved head SHA, and UTC timestamp. Do not include chat text or tokens. Add it after confirmed merge and then mark the local operation complete.

- [ ] **Step 5: Add the CLI command with mandatory SHA validation**

`argparse` must reject a missing `--expected-head`; runtime must reject anything not matching `[0-9a-fA-F]{40}` before obtaining a token or contacting GitHub. The CLI must not contain `--yes`, `--latest`, `--force`, or repository-wide approval options for merge.

- [ ] **Step 6: Run merge tests and ensure Worker imports no merge method**

Run: `.venv/bin/pytest tests/test_assisted_merge.py tests/test_cli.py tests/test_worker_service.py tests/test_daemon.py -q`

Expected: all tests PASS.

Run: `! rg -n "merge_pull_request|/merge" src/codex_mac_worker/worker.py src/codex_mac_worker/daemon.py src/codex_mac_worker/durable_github.py`

Expected: command exits 0 because the Worker execution path contains no merge call.

- [ ] **Step 7: Commit**

```bash
git add src/codex_mac_worker/assisted_merge.py src/codex_mac_worker/cli.py tests/test_assisted_merge.py tests/test_cli.py
git commit -m "feat: add explicit approval-bound squash merge"
```

---

### Task 9: Update the dispatch skill, installer, scripts, and operator documentation

**Files:**
- Modify: `skills/dispatch-codex-task/SKILL.md`
- Modify: `skills/dispatch-codex-task/agents/openai.yaml`
- Modify: `scripts/bootstrap_repository.sh`
- Modify: `docs/MACBOOK_SETUP.md`
- Modify: `docs/MAC_MINI_SETUP.md`
- Modify: `docs/OPERATIONS.md`
- Modify: `docs/SECURITY.md`
- Modify: `README.md`
- Modify: `tests/test_dispatch_skill.py`
- Modify: `tests/test_operational_assets.py`

**Interfaces:**
- Produces: conversational repository onboarding, review, and merge workflow.
- Consumes: all new `codexctl` commands.

- [ ] **Step 1: Write failing skill-contract assertions**

```python
for required in (
    "codexctl repo status",
    "codexctl repo onboard",
    "codexctl repo finalize",
    "codexctl task review",
    "codexctl task merge",
    "expected-head",
    "head SHA",
    "explicit",
    "Goal",
):
    assert required in body
assert "future PR" in body
assert "automatic merge" in body
```

- [ ] **Step 2: Rewrite the skill safety contract**

The skill must use this interaction sequence:

1. `repo status` before dispatch; refuse task creation unless phase is `ready`.
2. `repo onboard` may prepare/adopt a PR and show its full immutable snapshot.
3. Stop after showing the snapshot. Only an explicit approval naming that PR authorizes `repo finalize` with the displayed head SHA.
4. `task review` is always read-only and displays gates, tests, acceptance evidence, risks, and approval fingerprint.
5. Stop after review. Only an explicit approval naming that PR or unambiguously referring to the current snapshot authorizes `task merge` with the displayed head SHA.
6. Any SHA/check/ruleset change requires a new review and new approval.
7. Never treat design approval, repository-wide approval, “看起来可以”, or old-thread approval as merge authorization.
8. Never use Goal mode or authorize future PRs.

- [ ] **Step 3: Update installation and operation documentation**

Document:

- one-time Mac mini upgrade enabling installation discovery;
- App installations remain explicitly repository-scoped unless the user deliberately selects all personal repositories;
- `repo status/onboard/finalize` examples;
- the `awaiting-worker` probe phase;
- normal `task review` then explicit approval then `task merge` flow;
- personal `gh` token stays on MacBook;
- Worker App has Contents/Issues/PR write for branches and artifacts, while the Ruleset prevents it from updating default branches;
- bootstrap onboarding is the only self-authored exception;
- no Goal mode, production deploy, automatic future approval, or Worker merge.

- [ ] **Step 4: Convert the old bootstrap script into a compatibility wrapper**

Keep argument validation and `gh auth status`, then print a deprecation message and execute:

```bash
exec codexctl repo status "$REPO"
```

Do not continue maintaining a second implementation of label colors or Ruleset behavior in shell.

- [ ] **Step 5: Run skill, docs, and shell checks**

Run: `.venv/bin/pytest tests/test_dispatch_skill.py tests/test_operational_assets.py -q`

Expected: all tests PASS.

Run: `bash -n scripts/bootstrap_repository.sh scripts/install_macbook.sh scripts/install_macos.sh`

Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add README.md docs skills/dispatch-codex-task scripts/bootstrap_repository.sh tests/test_dispatch_skill.py tests/test_operational_assets.py
git commit -m "docs: define repository onboarding and assisted merge operations"
```

---

### Task 10: Full regression, security inspection, and EaseWise rollout

**Files:**
- Modify only if verification exposes a defect: files owned by Tasks 1-9 and the corresponding focused test.
- No live EaseWise product source changes in this task.

**Interfaces:**
- Consumes: the complete implementation.
- Produces: a verified release commit, installed MacBook CLI/skill, upgraded Mac mini Worker, and a ready EaseWise repository.

- [ ] **Step 1: Run the complete local suite**

Run: `.venv/bin/pytest -q`

Expected: all tests PASS with zero failures.

- [ ] **Step 2: Run static security and packaging checks**

Run: `git diff --check origin/main...HEAD`

Expected: no output and exit 0.

Run: `! rg -n -- "--yolo|Goal mode enabled|merge_pull_request" src/codex_mac_worker/worker.py src/codex_mac_worker/daemon.py src/codex_mac_worker/durable_github.py`

Expected: exit 0.

Run: `.venv/bin/pip wheel --no-deps -w /tmp/codex-worker-wheel .`

Expected: one wheel is built successfully and includes `assets/codex-task.yml` plus `assets/codex-worker-watchdog.yml`.

- [ ] **Step 3: Review the implementation against every design acceptance criterion**

Record evidence in the PR description for: exact onboarding paths, App-only discovery, probe without Codex, SHA drift rejection, every merge blocker, no Worker merge call, personal-token isolation, idempotent write recovery, and no Goal mode. A missing item blocks rollout.

- [ ] **Step 4: Commit any verification-only corrections, then publish one implementation PR**

```bash
git status --short
git push -u origin codex/repository-onboarding-assisted-merge
gh pr create --draft --base main --head codex/repository-onboarding-assisted-merge --title "feat: add repository onboarding and approval-bound merge" --body-file /tmp/codex-worker-pr.md
```

Expected: one Draft PR URL. Do not merge it until its checks and review are complete.

- [ ] **Step 5: After implementation PR approval, upgrade MacBook**

```bash
git switch main
git pull --ff-only
./scripts/install_macbook.sh
codexctl repo status qiaozhang1225/EaseWise
```

Expected: command reports the current onboarding blockers without changing EaseWise.

- [ ] **Step 6: Upgrade Mac mini once**

On Mac mini, with no active Worker task:

```bash
cd "/path/to/codex-mac-worker"
git switch main
git pull --ff-only
./scripts/install_macos.sh
sudo launchctl kickstart -k system/com.easewise.codex-worker
sudo launchctl print system/com.easewise.codex-worker
```

Expected: service is running, configuration check shows `discover_installation_repositories: true`, and no existing task is duplicated. This is the only Mac mini operation needed for future App-authorized repository discovery.

- [ ] **Step 7: Adopt and review EaseWise PR #1**

```bash
codexctl repo onboard --repo qiaozhang1225/EaseWise --adopt-pr 1 | tee /tmp/easewise-onboarding.json
jq -r '.head_sha' /tmp/easewise-onboarding.json
```

Expected: exact paths are the three standard files, PR is Draft and clean, and head SHA is `e57072e03a99486d711cbb926ae64dd0ee771cc8` unless GitHub reports a newer SHA. If it is newer, use only the newly displayed SHA after reviewing its diff.

- [ ] **Step 8: Stop for explicit user approval, then finalize the approved SHA**

After the user explicitly approves EaseWise PR #1 in the current conversation, run:

```bash
approved_head="$(jq -r '.head_sha' /tmp/easewise-onboarding.json)"
test "${#approved_head}" -eq 40
codexctl repo finalize https://github.com/qiaozhang1225/EaseWise/pull/1 --expected-head "$approved_head"
```

Expected: onboarding PR is squash-merged, labels and Ruleset are reconciled, one probe Issue is queued, and the first report is `awaiting-worker` or `ready`.

- [ ] **Step 9: Wait for Worker attestation and verify readiness**

```bash
codexctl repo status qiaozhang1225/EaseWise
```

Expected within the normal 60-second poll window plus GitHub latency: phase `ready`, with files, labels, Ruleset, and Worker attestation all true.

- [ ] **Step 10: Run a minimal connectivity task before product code**

Dispatch a low-risk documentation-only Issue whose allowed path is one disposable test document, verify the Mac mini creates a Draft PR, run `codexctl task review`, explicitly approve its displayed PR/head, and run `codexctl task merge` with that exact SHA. Confirm the Worker closes the Issue as `codex:completed`.

- [ ] **Step 11: Dispatch the Four Pillars card layout fix**

Create a bounded task with:

```yaml
objective: Rebalance the Four Pillars card in 我的评测记录 so its details use the available width without regressing 手机号 or 梅花易数 cards
acceptance:
  - The Four Pillars card header keeps type/date and status readable without squeezing the details column
  - Four Pillars details span the available card width at desktop and mobile breakpoints
  - 手机号 and 梅花易数 card layouts remain unchanged
  - The configured frontend lint and build commands pass
allowed_paths:
  - product/frontend/src/components/profile/Profile.vue
verification_profile: full
risk: low
```

Use the current pushed default-branch SHA and committed product context files. Review and merge its resulting Worker PR only through the new immutable approval flow.

---

## Execution Checkpoints

- Checkpoint A after Task 3: Mac mini can discover an App-installed, config-opted-in repository and attest a probe without invoking Codex.
- Checkpoint B after Task 5: onboarding preparation/finalization/readiness is fully testable with fake GitHub and no live writes.
- Checkpoint C after Task 8: task review and one-SHA merge authorization are complete; Worker code still contains no merge path.
- Checkpoint D after Task 9: user-facing skill and operational documentation encode the same approval semantics as code.
- Live rollout begins only after the full suite and implementation PR review in Task 10.
