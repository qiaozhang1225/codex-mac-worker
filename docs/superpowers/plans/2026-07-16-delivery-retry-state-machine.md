# Delivery Retry State Machine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the exact verified delivery commit and let an explicitly approved retry finish push and Draft PR creation without rerunning Codex.

**Architecture:** `EventStore` owns a durable delivery checkpoint and command ledger, `GitOperations` supplies read-only commit integrity checks and a bounded push deadline, and `WorkerService` separates execution from delivery. The daemon routes `retry` only to `retry_delivery`, which validates an existing checkpoint or strictly reconstructs one retained legacy task before repeating verification, push, and idempotent Draft PR reconciliation.

**Tech Stack:** Python 3.12, SQLite WAL, Git CLI, pytest, GitHub App installation tokens, durable GitHub outbox.

## Global Constraints

- Do not use Codex Goal mode.
- A delivery retry must never call or resume the Codex runner.
- Every delivery retry requires a new authorized command ID and explicit user approval.
- The retry reuses one exact Worker-created commit; it never resets, amends, or replaces it.
- The task block, branch, worktree, HEAD, sole parent, project configuration hash, scope, secrets, binary limits, and verification commands must all be revalidated before a network write.
- The complete retry has a fresh 30-minute hard timeout that heartbeats and transport retries cannot extend.
- Only classified transient Git or GitHub delivery errors remain retryable.
- Authentication, authorization, policy, integrity, configuration, and verification failures are permanent.
- GitHub App credentials remain in the existing temporary Askpass or API client memory and never enter checkpoints, logs, Issues, PR bodies, or Codex.
- Draft PR creation remains idempotent through `DurableGitHub` head-branch reconciliation.
- The Worker never calls the merge API and never deploys production.
- Do not directly edit or delete SQLite or outbox rows during development, deployment, or Issue #12 recovery.
- The already executed Issue #12 retry command is never replayed; deployment requires a new explicit retry command.

---

### Task 1: Persist delivery checkpoints and inspect command records

**Files:**
- Modify: `src/codex_mac_worker/store.py:20-371`
- Modify: `tests/test_store.py:1-83`

**Interfaces:**
- Produces: `EventStore.save_delivery_checkpoint(repo, issue_number, task_hash, branch, worktree, context_commit, commit_sha, project_config_hash, verification_profile, verification_commands, verification_result, structured_result, model, cli_version, session_id) -> None`
- Produces: `EventStore.get_delivery_checkpoint(repo: str, issue_number: int, task_hash: str) -> dict[str, Any] | None`
- Produces: `EventStore.set_delivery_checkpoint_state(repo, issue_number, task_hash, *, phase: str, retryable: bool, last_error: str | None) -> None`
- Produces: `EventStore.update_delivery_verification(repo, issue_number, task_hash, verification_result: dict[str, Any]) -> None`
- Produces: `EventStore.get_command(command_id: str) -> dict[str, Any] | None`
- Preserves: SQLite WAL, existing task/run/outbox schemas, and all public methods.

- [ ] **Step 1: Write failing checkpoint round-trip and state tests**

Add to `tests/test_store.py`:

```python
def checkpoint_payload(tmp_path: Path) -> dict[str, object]:
    return {
        "repo": "owner/repo",
        "issue_number": 12,
        "task_hash": "a" * 64,
        "branch": "codex/12-layout",
        "worktree": str(tmp_path / "worktree"),
        "context_commit": "1" * 40,
        "commit_sha": "2" * 40,
        "project_config_hash": "3" * 64,
        "verification_profile": "fast",
        "verification_commands": ("python -m pytest -q",),
        "verification_result": {
            "passed": True,
            "commands": [
                {"command": "python -m pytest -q", "exit_code": 0, "output": "1 passed"}
            ],
        },
        "structured_result": {
            "status": "completed",
            "acceptance_results": [],
            "risks": [],
            "needs_human": [],
        },
        "model": "gpt-test",
        "cli_version": "codex-test",
        "session_id": "session-1",
    }


def test_delivery_checkpoint_round_trips_and_updates_state(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "worker.sqlite3")
    payload = checkpoint_payload(tmp_path)

    store.save_delivery_checkpoint(**payload)
    store.set_delivery_checkpoint_state(
        "owner/repo",
        12,
        "a" * 64,
        phase="push",
        retryable=True,
        last_error="GitError: timed out",
    )

    checkpoint = store.get_delivery_checkpoint("owner/repo", 12, "a" * 64)
    assert checkpoint is not None
    assert checkpoint["verification_commands"] == ["python -m pytest -q"]
    assert checkpoint["verification_result"]["passed"] is True
    assert checkpoint["structured_result"]["status"] == "completed"
    assert checkpoint["phase"] == "push"
    assert checkpoint["retryable"] is True
    assert checkpoint["last_error"] == "GitError: timed out"


def test_delivery_checkpoint_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "worker.sqlite3"
    store = EventStore(path)
    store.save_delivery_checkpoint(**checkpoint_payload(tmp_path))
    store.close()

    checkpoint = EventStore(path).get_delivery_checkpoint("owner/repo", 12, "a" * 64)

    assert checkpoint is not None
    assert checkpoint["commit_sha"] == "2" * 40
```

- [ ] **Step 2: Write the failing command inspection test**

Extend `test_commands_execute_once`:

```python
pending = store.get_command("cmd-1")
assert pending is not None
assert pending["executed_at"] is None

store.mark_command_executed("cmd-1", "paused")
executed = store.get_command("cmd-1")
assert executed is not None
assert executed["result"] == "paused"
assert executed["executed_at"] is not None
```

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_store.py -q
```

Expected: failures because the checkpoint table and new EventStore methods do not exist.

- [ ] **Step 4: Add the checkpoint migration and JSON-safe accessors**

Add this table inside `_migrate()`:

```sql
CREATE TABLE IF NOT EXISTS delivery_checkpoints (
    repo TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    task_hash TEXT NOT NULL,
    branch TEXT NOT NULL,
    worktree TEXT NOT NULL,
    context_commit TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    project_config_hash TEXT NOT NULL,
    verification_profile TEXT NOT NULL,
    verification_commands_json TEXT NOT NULL,
    verification_result_json TEXT NOT NULL,
    structured_result_json TEXT NOT NULL,
    model TEXT,
    cli_version TEXT,
    session_id TEXT,
    phase TEXT NOT NULL DEFAULT 'checkpointed',
    retryable INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo, issue_number, task_hash)
);
```

Implement `save_delivery_checkpoint` with one SQLite upsert statement.
The conflict update may refresh evidence fields only while `commit_sha`, `context_commit`, branch,
and worktree exactly match the stored row; otherwise raise `ValueError("delivery checkpoint identity changed")` before writing. Encode each JSON field using `json.dumps(value, ensure_ascii=False, sort_keys=True)`.
On conflict, preserve the existing `phase`, `retryable`, `last_error`, and `created_at`; only
`set_delivery_checkpoint_state` may change delivery eligibility.

Implement the read and state methods with these exact shapes:

```python
def get_delivery_checkpoint(
    self, repo: str, issue_number: int, task_hash: str
) -> dict[str, Any] | None:
    row = self.connection.execute(
        "SELECT * FROM delivery_checkpoints WHERE repo=? AND issue_number=? AND task_hash=?",
        (repo, issue_number, task_hash),
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["verification_commands"] = json.loads(item.pop("verification_commands_json"))
    item["verification_result"] = json.loads(item.pop("verification_result_json"))
    item["structured_result"] = json.loads(item.pop("structured_result_json"))
    item["retryable"] = bool(item["retryable"])
    return item


def get_command(self, command_id: str) -> dict[str, Any] | None:
    row = self.connection.execute(
        "SELECT * FROM commands WHERE command_id=?", (command_id,)
    ).fetchone()
    return dict(row) if row else None
```

`set_delivery_checkpoint_state` must truncate `last_error` to 4000 characters and update
`phase`, `retryable`, `last_error`, and `updated_at` in one transaction. `update_delivery_verification`
must update only `verification_result_json` and `updated_at`.

- [ ] **Step 5: Run store tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_store.py -q
```

Expected: every store test passes, including reopen persistence and executed command inspection.

- [ ] **Step 6: Commit the persistence unit**

```bash
git add src/codex_mac_worker/store.py tests/test_store.py
git commit -m "feat: persist delivery checkpoints"
```

---

### Task 2: Add commit-integrity checks and a bounded push deadline

**Files:**
- Modify: `src/codex_mac_worker/gitops.py:46-377`
- Modify: `tests/test_gitops.py`

**Interfaces:**
- Produces: `GitOperations.is_clean(worktree: Path) -> bool`
- Produces: `GitOperations.commit_parents(worktree: Path, commit_sha: str) -> tuple[str, ...]` where the tuple contains every parent SHA in Git order.
- Extends: `GitOperations.push(worktree, *, branch, clone_url, token, deadline_monotonic: float | None = None) -> None`
- Extends internally: `_git(cwd, *args, env=None, check=True, timeout_seconds: float | None = None)` and `_git_network(cwd, *args, env=None, proxy_target_url=None, deadline_monotonic: float | None = None)`
- Preserves: proxy → direct → proxy ordering, three total attempts, and permanent-error short circuit.

- [ ] **Step 1: Write failing clean-worktree and sole-parent tests**

Add to `tests/test_gitops.py`:

```python
def test_git_reports_clean_state_and_commit_parents(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    (source / "one.txt").write_text("one\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "one")
    parent = git(source, "rev-parse", "HEAD")
    (source / "two.txt").write_text("two\n", encoding="utf-8")
    git(source, "add", ".")
    git(source, "commit", "-m", "two")
    head = git(source, "rev-parse", "HEAD")
    operations = GitOperations(cache_root=tmp_path / "cache", worktree_root=tmp_path / "trees")

    assert operations.is_clean(source) is True
    assert operations.commit_parents(source, head) == (parent,)

    (source / "two.txt").write_text("changed\n", encoding="utf-8")
    assert operations.is_clean(source) is False
```

- [ ] **Step 2: Write the failing expired-deadline push test**

```python
def test_push_rejects_expired_delivery_deadline(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    operations = GitOperations(cache_root=tmp_path / "cache", worktree_root=tmp_path / "trees")

    with pytest.raises(GitError, match="deadline",) as error:
        operations.push(
            source,
            branch="codex/12-task",
            clone_url="https://example.test/repo.git",
            token=None,
            deadline_monotonic=time.monotonic() - 1,
        )

    assert error.value.retryable is True
```

Add `import time` to the test module.

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_gitops.py -k 'clean_state or expired_delivery_deadline' -q
```

Expected: failures because the integrity helpers and deadline parameter do not exist.

- [ ] **Step 4: Implement read-only integrity helpers**

```python
def is_clean(self, worktree: Path) -> bool:
    return not self._git(worktree, "status", "--porcelain", "--untracked-files=all").stdout


def commit_parents(self, worktree: Path, commit_sha: str) -> tuple[str, ...]:
    fields = self._git(
        worktree, "rev-list", "--parents", "-n", "1", commit_sha
    ).stdout.strip().split()
    if not fields or fields[0] != commit_sha:
        raise GitError("delivery commit cannot be resolved")
    return tuple(fields[1:])
```

- [ ] **Step 5: Bound network Git by one monotonic deadline**

Add `timeout_seconds` to `_git`. Convert `subprocess.TimeoutExpired` to a retryable timeout result
without printing environment values:

```python
try:
    result = subprocess.run(
        [self.git_path, *args],
        cwd=cwd,
        env=merged_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
    )
except subprocess.TimeoutExpired as exc:
    stdout = exc.stdout if isinstance(exc.stdout, str) else ""
    stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    result = subprocess.CompletedProcess(
        [self.git_path, *args],
        124,
        stdout,
        (stderr + "\ngit delivery deadline expired").strip(),
    )
```

In `_git_network`, compute `remaining = deadline_monotonic - time.monotonic()` before each attempt
and before each retry sleep. Raise `GitError("git delivery deadline expired", retryable=True)` when
remaining is non-positive. Pass `timeout_seconds=remaining` to `_git` only when a deadline was
provided so existing monkeypatched call signatures remain compatible.

Thread the optional deadline through `push`:

```python
self._git_network(
    worktree,
    "push",
    remote_name,
    f"HEAD:refs/heads/{branch}",
    env=env,
    proxy_target_url=clone_url,
    deadline_monotonic=deadline_monotonic,
)
```

- [ ] **Step 6: Run all Git tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_gitops.py -q
```

Expected: all Git tests pass, including the existing authentication, retry classification, and
proxy route tests.

- [ ] **Step 7: Commit the Git integrity unit**

```bash
git add src/codex_mac_worker/gitops.py tests/test_gitops.py
git commit -m "feat: validate bounded delivery commits"
```

---

### Task 3: Checkpoint normal delivery and classify delivery failures

**Files:**
- Modify: `src/codex_mac_worker/worker.py:1-855`
- Modify: `tests/test_worker_service.py:1-590`

**Interfaces:**
- Produces internally: `_serialize_verification(result: VerificationResult) -> dict[str, Any]`
- Produces internally: `_project_config_hash(worktree: Path) -> str`
- Produces internally: `_set_delivery_failure(repo: str, issue_number: int, task_hash: str, phase: str, exc: Exception) -> None`
- Changes: `process_issue` commits, persists a checkpoint, then attempts push and Draft PR.
- Preserves: existing Codex attempts, verification policy, PR metadata, and task hard deadline.

- [ ] **Step 1: Write the failing transient-push checkpoint test**

Add `GitError` to the test imports. First extract these exact test helpers so later failure cases
reuse one setup without changing production code:

```python
def bounded_issue(sha: str) -> dict:
    return {
        "number": 12,
        "title": "Bounded task",
        "body": task_body(sha=sha),
        "labels": [{"name": "codex:queued"}],
        "user": {"login": "owner"},
    }


def worker_config(tmp_path: Path, remote: Path) -> WorkerConfig:
    return WorkerConfig(
        "mac-mini",
        60,
        120,
        tmp_path / "state.sqlite3",
        tmp_path / "cache",
        tmp_path / "worktrees",
        tmp_path / "outputs",
        Path("/tmp/codex"),
        "123",
        "456",
        tmp_path / "app.pem",
        ("owner",),
        (RepositoryConfig("owner/repo", str(remote)),),
    )


def make_service(
    config: WorkerConfig,
    github: FakeGitHub,
    store: EventStore,
    operations: GitOperations,
    runner: object,
) -> WorkerService:
    return WorkerService(
        config=config,
        github=github,
        token_provider=lambda: "token",
        store=store,
        git=operations,
        runner=runner,
    )
```

Then add:

```python
def test_transient_push_failure_persists_retryable_delivery_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, sha = make_project_remote(tmp_path)
    issue = bounded_issue(sha)
    github = FakeGitHub(issue)
    config = worker_config(tmp_path, remote)
    store = EventStore(config.database_path)
    operations = GitOperations(cache_root=config.cache_root, worktree_root=config.worktree_root)
    monkeypatch.setattr(
        operations,
        "push",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            GitError("connect timed out", retryable=True)
        ),
    )
    service = make_service(config, github, store, operations, FakeRunner())

    service.process_issue(config.repositories[0], issue)

    task = store.get_task("owner/repo", 12)
    assert task is not None and task["state"] == "needs-attention"
    checkpoint = store.get_delivery_checkpoint(
        "owner/repo", 12, parse_task_body(issue["body"]).task_hash
    )
    assert checkpoint is not None
    assert checkpoint["commit_sha"] == git(Path(task["worktree"]), "rev-parse", "HEAD")
    assert checkpoint["phase"] == "push"
    assert checkpoint["retryable"] is True
    assert checkpoint["model"] == "gpt-test"
```

Replace only repeated setup in touched tests; do not rewrite unrelated fixtures.

- [ ] **Step 2: Write failing permanent-push and transient-PR tests**

```python
@pytest.mark.parametrize("retryable", [False, True])
def test_delivery_failure_classification_is_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retryable: bool
) -> None:
    remote, sha = make_project_remote(tmp_path)
    issue = bounded_issue(sha)
    github = FakeGitHub(issue)
    config = worker_config(tmp_path, remote)
    store = EventStore(config.database_path)
    operations = GitOperations(cache_root=config.cache_root, worktree_root=config.worktree_root)
    if retryable:
        def fail_pr(*args: object, **kwargs: object) -> dict:
            raise GitHubError("service unavailable", status_code=503, retryable=True)
        monkeypatch.setattr(github, "create_draft_pr", fail_pr)
    else:
        monkeypatch.setattr(
            operations,
            "push",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                GitError("authentication failed", retryable=False)
            ),
        )
    service = make_service(config, github, store, operations, FakeRunner())

    service.process_issue(config.repositories[0], issue)

    checkpoint = store.get_delivery_checkpoint(
        "owner/repo", 12, parse_task_body(issue["body"]).task_hash
    )
    assert checkpoint is not None
    assert checkpoint["retryable"] is retryable
    assert checkpoint["phase"] == ("pull-request" if retryable else "push")
```

Import `GitHubError` from `codex_mac_worker.github`.

- [ ] **Step 3: Run the new Worker tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_service.py -k 'checkpoint or failure_classification' -q
```

Expected: failures because normal delivery does not persist evidence or classify its delivery
phase.

- [ ] **Step 4: Add deterministic checkpoint serialization helpers**

In `WorkerService`, serialize verification without credentials or environment data:

```python
def _serialize_verification(self, result: VerificationResult) -> dict[str, Any]:
    return {
        "passed": result.passed,
        "termination_reason": result.termination_reason,
        "commands": [
            {
                "command": item.command,
                "exit_code": item.exit_code,
                "output": item.output[-3000:],
            }
            for item in result.commands
        ],
    }


def _project_config_hash(self, worktree: Path) -> str:
    text = (worktree / ".codex-worker" / "project.toml").read_text(encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
```

Add a `_set_delivery_failure` helper that treats only `exc.retryable is True` as retryable:

```python
def _set_delivery_failure(
    self, repo: str, issue_number: int, task_hash: str, phase: str, exc: Exception
) -> None:
    self.store.set_delivery_checkpoint_state(
        repo,
        issue_number,
        task_hash,
        phase=phase,
        retryable=getattr(exc, "retryable", False) is True,
        last_error=f"{type(exc).__name__}: {exc}",
    )
```

- [ ] **Step 5: Persist before the first push and classify each delivery phase**

Immediately after `self.git.commit` returns the delivery SHA, call `save_delivery_checkpoint` with the exact branch,
worktree, context commit, commit SHA, project config hash, profile commands,
`_serialize_verification(verification_result)`, structured result, model, CLI version, and session.

Wrap phases independently:

```python
try:
    self.git.push(
        prepared.path,
        branch=branch,
        clone_url=repository.clone_url,
        token=self.token_provider(),
    )
except Exception as exc:
    self._set_delivery_failure(repo, number, task_hash, "push", exc)
    raise

try:
    pr = self.github.create_draft_pr(
        repo,
        branch,
        spec.base_branch,
        f"[Codex #{number}] {issue.get('title', 'Task')}",
        pr_body,
    )
except Exception as exc:
    self._set_delivery_failure(repo, number, task_hash, "pull-request", exc)
    raise
```

Wrap label, task, and status-comment finalization as phase `finalize`; a retryable GitHub error in
that phase remains delivery-retryable because Draft PR reconciliation is idempotent. After the PR
and task-state writes succeed, mark the checkpoint `phase="complete"`,
`retryable=False`, and `last_error=None`. The outer exception handler remains responsible for
the normal `needs-attention` task label and status comment.

- [ ] **Step 6: Run focused and full Worker service tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_service.py -q
```

Expected: every Worker service test passes; successful delivery has a complete checkpoint and
failed delivery preserves the local commit.

- [ ] **Step 7: Commit normal checkpoint creation**

```bash
git add src/codex_mac_worker/worker.py tests/test_worker_service.py
git commit -m "feat: checkpoint verified Worker delivery"
```

---

### Task 4: Retry an existing checkpoint without invoking Codex

**Files:**
- Modify: `src/codex_mac_worker/worker.py:18-855`
- Modify: `tests/test_worker_service.py`

**Interfaces:**
- Produces: `WorkerService.retry_delivery(repository: RepositoryConfig, issue: dict[str, Any]) -> str`
- Produces internally: `_restore_verification(payload: dict[str, Any]) -> VerificationResult`
- Produces internally: `_validate_checkpoint(repository, issue, task, checkpoint) -> tuple[TaskSpec, ProjectConfig, Path]`
- Produces internally: `_deliver_checkpoint(repository, issue, spec, checkpoint, verification_result, *, deadline_monotonic) -> dict[str, Any]`
- Returns stable command results: `awaiting-review`, `paused`, `cancelled`, `needs-attention`, or `not-retryable`.

- [ ] **Step 1: Write the failing retry-success test with a runner that must not run**

Add this fixture helper:

```python
def prepare_transient_push_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[WorkerService, WorkerConfig, FakeGitHub, EventStore, dict]:
    remote, sha = make_project_remote(tmp_path)
    issue = bounded_issue(sha)
    github = FakeGitHub(issue)
    config = worker_config(tmp_path, remote)
    store = EventStore(config.database_path)
    operations = GitOperations(
        cache_root=config.cache_root, worktree_root=config.worktree_root
    )
    real_push = operations.push

    def fail_push(*args: object, **kwargs: object) -> None:
        raise GitError("connect timed out", retryable=True)

    monkeypatch.setattr(operations, "push", fail_push)
    service = make_service(config, github, store, operations, FakeRunner())
    service.process_issue(config.repositories[0], issue)
    monkeypatch.setattr(operations, "push", real_push)
    return service, config, github, store, issue
```

Then add:

```python
def test_retry_delivery_reuses_checkpoint_without_running_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, github, store, issue = prepare_transient_push_failure(
        tmp_path, monkeypatch
    )
    original = store.get_delivery_checkpoint(
        "owner/repo", 12, parse_task_body(issue["body"]).task_hash
    )
    assert original is not None and original["retryable"] is True

    class MustNotRun:
        def run(self, *args: object, **kwargs: object) -> RunnerResult:
            raise AssertionError("delivery retry invoked Codex")

    service.runner = MustNotRun()
    outcome = service.retry_delivery(config.repositories[0], issue)

    assert outcome == "awaiting-review"
    assert store.get_task("owner/repo", 12)["pr_number"] == 44
    assert git(tmp_path / "remote.git", "rev-parse", "codex/12-bounded-task") == original["commit_sha"]
    assert store.get_delivery_checkpoint(
        "owner/repo", 12, parse_task_body(issue["body"]).task_hash
    )["retryable"] is False
```

- [ ] **Step 2: Write failing drift-rejection tests before network access**

Parameterize these mutations against the retained checkpoint:

```python
@pytest.mark.parametrize(
    "mutation, expected",
    [
        ("task-body", "task body changed"),
        ("branch", "branch changed"),
        ("head", "HEAD changed"),
        ("dirty", "worktree is not clean"),
        ("project-config", "project config changed"),
    ],
)
def test_retry_delivery_rejects_integrity_drift_before_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected: str,
) -> None:
    service, config, github, store, issue = prepare_transient_push_failure(
        tmp_path, monkeypatch
    )
    task = store.get_task("owner/repo", 12)
    apply_checkpoint_mutation(
        mutation,
        Path(task["worktree"]),
        issue,
        store,
        parse_task_body(issue["body"]).task_hash,
    )
    pushed = False

    def forbidden_push(*args: object, **kwargs: object) -> None:
        nonlocal pushed
        pushed = True

    monkeypatch.setattr(service.git, "push", forbidden_push)

    assert service.retry_delivery(config.repositories[0], issue) == "not-retryable"
    assert pushed is False
    assert expected in github.comments[-1]
```

Use this helper; it uses only Git in the temporary fixture and EventStore public methods:

```python
def apply_checkpoint_mutation(
    mutation: str,
    worktree: Path,
    issue: dict,
    store: EventStore,
    task_hash: str,
) -> None:
    if mutation == "task-body":
        issue["body"] = issue["body"].replace("Unit tests pass", "Changed criterion")
        return
    if mutation == "branch":
        git(worktree, "switch", "-c", "unexpected-branch")
        return
    if mutation == "head":
        git(worktree, "config", "user.name", "Test")
        git(worktree, "config", "user.email", "test@example.com")
        git(worktree, "commit", "--allow-empty", "-m", "unexpected")
        return
    if mutation == "dirty":
        (worktree / "src" / "result.txt").write_text("dirty\n", encoding="utf-8")
        return
    if mutation == "project-config":
        checkpoint = store.get_delivery_checkpoint("owner/repo", 12, task_hash)
        assert checkpoint is not None
        store.save_delivery_checkpoint(
            repo=checkpoint["repo"],
            issue_number=checkpoint["issue_number"],
            task_hash=checkpoint["task_hash"],
            branch=checkpoint["branch"],
            worktree=checkpoint["worktree"],
            context_commit=checkpoint["context_commit"],
            commit_sha=checkpoint["commit_sha"],
            project_config_hash="f" * 64,
            verification_profile=checkpoint["verification_profile"],
            verification_commands=tuple(checkpoint["verification_commands"]),
            verification_result=checkpoint["verification_result"],
            structured_result=checkpoint["structured_result"],
            model=checkpoint["model"],
            cli_version=checkpoint["cli_version"],
            session_id=checkpoint["session_id"],
        )
        return
    raise AssertionError(f"unknown mutation: {mutation}")
```

Test the sole-parent guard independently without rewriting the retained commit:

```python
def test_retry_delivery_rejects_multiple_parents_before_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, github, _, issue = prepare_transient_push_failure(tmp_path, monkeypatch)
    spec = parse_task_body(issue["body"])
    monkeypatch.setattr(
        service.git,
        "commit_parents",
        lambda worktree, commit_sha: (spec.context_commit, "4" * 40),
    )
    monkeypatch.setattr(
        service.git,
        "push",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("push called")),
    )

    assert service.retry_delivery(config.repositories[0], issue) == "not-retryable"
    assert "sole parent" in github.comments[-1]
```

Do not issue raw `UPDATE` or `DELETE` statements against SQLite in these tests.

- [ ] **Step 3: Write failing verification and deadline tests**

```python
def test_retry_delivery_stops_when_fresh_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, github, store, issue = prepare_transient_push_failure(tmp_path, monkeypatch)
    monkeypatch.setattr(
        worker_module,
        "run_verification",
        lambda *args, **kwargs: VerificationResult(
            False, (CommandResult("pytest", 1, "failed"),)
        ),
    )
    monkeypatch.setattr(
        service.git,
        "push",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("push called")),
    )

    assert service.retry_delivery(config.repositories[0], issue) == "not-retryable"
    checkpoint = store.get_delivery_checkpoint(
        "owner/repo", 12, parse_task_body(issue["body"]).task_hash
    )
    assert checkpoint["retryable"] is False
```

Patch a module constant `DELIVERY_RETRY_TIMEOUT_SECONDS = 1800` to zero in a second test and
assert `retry_delivery` returns `not-retryable` without calling push or Codex.

Also prove an ambiguous PR response cannot duplicate a PR. Import `DurableGitHub` and add:

```python
def test_retry_delivery_reconciles_existing_pr_after_ambiguous_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, config, _, store, issue = prepare_transient_push_failure(tmp_path, monkeypatch)

    class AmbiguousRemote(FakeGitHub):
        def __init__(self, current_issue: dict) -> None:
            super().__init__(current_issue)
            self.existing: dict | None = None
            self.create_calls = 0

        def find_open_pull_request(self, repo: str, head: str) -> dict | None:
            return self.existing if self.existing and self.existing["head"] == head else None

        def create_draft_pr(
            self, repo: str, head: str, base: str, title: str, body: str
        ) -> dict:
            self.create_calls += 1
            self.existing = super().create_draft_pr(repo, head, base, title, body)
            raise GitHubError("response lost", status_code=None, retryable=True)

    remote = AmbiguousRemote(issue)
    service.github = DurableGitHub(remote, store)

    assert service.retry_delivery(config.repositories[0], issue) == "needs-attention"
    assert service.retry_delivery(config.repositories[0], issue) == "awaiting-review"
    assert remote.create_calls == 1
    assert len(remote.prs) == 1
```

- [ ] **Step 4: Implement checkpoint restoration and integrity validation**

Import `CommandResult`, `VerificationResult`, and `ProjectConfig`. Restore only the fields stored
by Task 3:

```python
def _restore_verification(self, payload: dict[str, Any]) -> VerificationResult:
    commands = tuple(
        CommandResult(
            command=str(item["command"]),
            exit_code=int(item["exit_code"]),
            output=str(item["output"]),
        )
        for item in payload["commands"]
    )
    return VerificationResult(
        passed=bool(payload["passed"]),
        commands=commands,
        termination_reason=payload.get("termination_reason"),
    )
```

`_validate_checkpoint` must perform these checks in this order:

1. authorized Issue author and valid current repository authority;
2. parsed task hash equals the task row and checkpoint task hash;
3. task branch and worktree exactly equal the checkpoint;
4. worktree exists, `current_branch == checkpoint.branch`, `is_clean is True`;
5. `current_head == checkpoint.commit_sha`;
6. `commit_parents(commit_sha) == (spec.context_commit,)`;
7. retained project config parses, is bound to the Worker App, and its raw-text SHA-256 equals
   `checkpoint.project_config_hash`;
8. `checkpoint.verification_profile == spec.verification_profile` and checkpoint commands exactly
   equal `project_config.verification[spec.verification_profile]`;
9. committed diff is non-empty and passes `validate_changed_paths` and `scan_for_secrets`.

Any mismatch raises `PolicyError` with the specific messages asserted by the tests.

- [ ] **Step 5: Implement one bounded retry and shared idempotent delivery**

Start `deadline = time.monotonic() + DELIVERY_RETRY_TIMEOUT_SECONDS`. Fetch the checkpoint and
reject it unless `retryable is True`. Set the remote and local task state to `retrying`, add one
status comment containing the checkpoint phase and fixed hard deadline, validate, then run only
the checkpoint-recorded profile:

```python
remaining = deadline - time.monotonic()
if remaining <= 0:
    raise RunnerTimeout("delivery retry hard timeout exceeded before verification")
verification_result = run_verification(
    worktree,
    project_config,
    spec.verification_profile,
    timeout_seconds=min(remaining, 1800),
    codex_path=self.config.codex_path if self.config.codex_home else None,
    codex_home=self.config.codex_home,
    control_callback=self._command_monitor(repo, number),
)
```

Handle `pause` and `cancel` as stable outcomes without clearing retry eligibility. A failed or
expired verification is permanent and returns `not-retryable` after `_mark_attention`.

Refactor normal delivery into `_deliver_checkpoint`. Recreate `RunnerResult` from checkpoint
model, CLI version, session ID, and serialized structured result only for `_delivery_pr_body`;
do not call `runner.run`. Push with `deadline_monotonic=deadline`, create/reconcile the Draft PR,
update the task and checkpoint, update the same status comment to `awaiting-review`, and return the
PR payload. Status text may include phase and sanitized exception text but never tokens, Askpass
paths, environment variables, or private-key paths.

Catch push, PR, and final GitHub state errors by phase. A transient exception keeps the checkpoint
retryable and returns `needs-attention`; a permanent exception clears eligibility and returns
`not-retryable`.

- [ ] **Step 6: Run retry tests and Worker service regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_service.py -k 'retry_delivery' -q
.venv/bin/python -m pytest tests/test_worker_service.py -q
```

Expected: all retry scenarios and all existing execution/revision scenarios pass.

- [ ] **Step 7: Commit the dedicated retry path**

```bash
git add src/codex_mac_worker/worker.py tests/test_worker_service.py
git commit -m "feat: retry checkpointed delivery only"
```

---

### Task 5: Strictly reconstruct retained pre-checkpoint deliveries

**Files:**
- Modify: `src/codex_mac_worker/worker.py`
- Modify: `tests/test_worker_service.py`

**Interfaces:**
- Produces internally: `_legacy_checkpoint_candidate(repository, issue, task, spec) -> dict[str, Any]`
- Uses: `legacy-delivery-recovery:{repo}#{issue_number}:{task_hash}` Worker state marker.
- Preserves: new tasks must have a checkpoint before their first push.

- [ ] **Step 1: Write the failing strict legacy reconstruction success test**

Build a fixture that follows the Issue #12 evidence shape without calling the new normal
checkpoint writer: create the task worktree from the context commit, run `FakeRunner`, record one
successful EventStore run with matching `session_id`, commit the generated change once, and upsert
the task as `needs-attention` with its branch, worktree, and session ID.

Add this complete test fixture helper:

```python
def prepare_legacy_delivery(
    tmp_path: Path,
    *,
    damage: str | None = None,
) -> tuple[WorkerService, WorkerConfig, FakeGitHub, EventStore, dict, str]:
    remote, sha = make_project_remote(tmp_path)
    if damage == "verification-failure":
        source = tmp_path / "source"
        config_path = source / ".codex-worker" / "project.toml"
        config_path.write_text(
            project_config_text().replace(
                f"{sys.executable} -c 'print(123)'",
                f"{sys.executable} -c 'raise SystemExit(1)'",
            ),
            encoding="utf-8",
        )
        git(source, "add", str(config_path.relative_to(source)))
        git(source, "commit", "-m", "failing baseline verification")
        git(source, "push", str(remote), "HEAD:main")
        sha = git(source, "rev-parse", "HEAD")

    issue = bounded_issue(sha)
    github = FakeGitHub(issue)
    config = worker_config(tmp_path, remote)
    store = EventStore(config.database_path)
    operations = GitOperations(
        cache_root=config.cache_root, worktree_root=config.worktree_root
    )
    service = make_service(config, github, store, operations, FakeRunner())
    mirror = operations.ensure_mirror("owner/repo", str(remote), token="token")
    prepared = operations.prepare_worktree(
        repo="owner/repo",
        mirror=mirror,
        context_commit=sha,
        base_branch="main",
        issue_number=12,
        slug="bounded-task",
    )
    result = FakeRunner().run(prepared.path, "prompt", tmp_path / "schema.json")
    if damage == "scope-violation":
        (prepared.path / ".env").write_text(
            'PASSWORD="abcdefghijklmnop"\n', encoding="utf-8"
        )
    commit_sha = operations.commit(
        prepared.path,
        "feat: complete codex task #12",
        author_name="Codex Mac Worker",
        author_email="codex-worker@users.noreply.github.com",
    )
    if damage == "wrong-parent":
        git(prepared.path, "commit", "--allow-empty", "-m", "second delivery commit")
        commit_sha = git(prepared.path, "rev-parse", "HEAD")

    if damage != "missing-run":
        run_id = store.start_run("owner/repo", 12)
        store.finish_run(
            run_id,
            exit_code=1 if damage == "nonzero-run" else 0,
            result={
                "session_id": result.session_id,
                "termination_reason": result.termination_reason,
                "event_count": len(result.events),
                "last_message": "{" if damage == "invalid-final-message" else result.last_message,
                "model": result.model,
                "cli_version": result.cli_version,
            },
        )
    task_session = "different-session" if damage == "session-mismatch" else result.session_id
    store.upsert_task(
        repo="owner/repo",
        issue_number=12,
        task_hash=parse_task_body(issue["body"]).task_hash,
        state="needs-attention",
        branch=prepared.branch,
        worktree=str(prepared.path),
        session_id=task_session,
    )

    if damage == "dirty-worktree":
        (prepared.path / "src" / "result.txt").write_text("dirty\n", encoding="utf-8")
    elif damage == "wrong-branch":
        git(prepared.path, "switch", "-c", "unexpected-branch")
    elif damage == "missing-worktree":
        prepared.path.rename(prepared.path.with_name(prepared.path.name + "-moved"))

    return service, config, github, store, issue, commit_sha
```

Import `sys` if it is not already present in the test module.

```python
def test_retry_delivery_reconstructs_strict_legacy_checkpoint_once(
    tmp_path: Path,
) -> None:
    service, config, github, store, issue, commit_sha = prepare_legacy_delivery(tmp_path)

    outcome = service.retry_delivery(config.repositories[0], issue)

    assert outcome == "awaiting-review"
    checkpoint = store.get_delivery_checkpoint(
        "owner/repo", 12, parse_task_body(issue["body"]).task_hash
    )
    assert checkpoint is not None
    assert checkpoint["commit_sha"] == commit_sha
    assert store.get_worker_state(
        f"legacy-delivery-recovery:owner/repo#12:{checkpoint['task_hash']}"
    ) == "reconstructed"
    assert git(tmp_path / "remote.git", "rev-parse", "codex/12-bounded-task") == commit_sha
```

- [ ] **Step 2: Write failing rejection tests for incomplete legacy evidence**

Parameterize these missing or ambiguous conditions:

```python
@pytest.mark.parametrize(
    "damage",
    [
        "missing-worktree",
        "dirty-worktree",
        "wrong-branch",
        "wrong-parent",
        "missing-run",
        "nonzero-run",
        "session-mismatch",
        "invalid-final-message",
        "scope-violation",
        "verification-failure",
    ],
)
def test_legacy_reconstruction_rejects_incomplete_evidence(
    tmp_path: Path, damage: str
) -> None:
    service, config, github, store, issue, _ = prepare_legacy_delivery(tmp_path, damage=damage)

    outcome = service.retry_delivery(config.repositories[0], issue)

    assert outcome == "not-retryable"
    assert store.get_delivery_checkpoint(
        "owner/repo", 12, parse_task_body(issue["body"]).task_hash
    ) is None
    assert len(github.prs) == 0
```

- [ ] **Step 3: Run legacy tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_service.py -k 'legacy_reconstruction' -q
```

Expected: failures because no strict reconstruction path exists.

- [ ] **Step 4: Build a candidate only from matching durable evidence**

When no checkpoint exists, `_legacy_checkpoint_candidate` must require:

```python
runs = [
    run
    for run in self.store.list_runs(repo, number)
    if run["finished_at"]
    and run["exit_code"] == 0
    and isinstance(run.get("result"), dict)
    and run["result"].get("termination_reason") is None
    and run["result"].get("session_id") == task.get("session_id")
]
if len(runs) != 1:
    raise PolicyError("legacy delivery requires one matching successful run")
```

Require the same task hash in the parsed Issue and task row. Restore a `RunnerResult` from the
single run and validate its `last_message` with `_require_completed_result`. Derive the candidate
branch, worktree, context commit, current HEAD, project config hash, profile commands, structured
result, model, CLI version, and session ID. Do not accept a missing or truncated final message.

Strengthen `_require_completed_result` for both new and legacy runs so it enforces the complete
local shape that was supplied to Codex:

```python
required_keys = {
    "status",
    "summary",
    "changed_files",
    "risks",
    "needs_human",
    "acceptance_results",
}
if set(payload) != required_keys:
    raise PolicyError("Codex result fields do not match the frozen result schema")
if not isinstance(payload["summary"], str) or not payload["summary"].strip():
    raise PolicyError("Codex result summary must not be empty")
if not isinstance(payload["changed_files"], list) or not all(
    isinstance(item, str) for item in payload["changed_files"]
):
    raise PolicyError("Codex changed_files must be a string list")
```

Keep the existing status, acceptance order/count, evidence, risks, and human-dependency checks.

- [ ] **Step 5: Validate and persist the legacy candidate before delivery**

Reuse Task 4's complete integrity and fresh-verification path. Persist the candidate only after
all local guards and verification pass, then set:

```python
self.store.set_worker_state(legacy_key, "reconstructed")
```

On any permanent reconstruction rejection, set `legacy_key` to `"rejected"`, mark the task
`needs-attention`, and return `not-retryable`. If that key is already `"rejected"`, do not attempt
reconstruction again. A process crash before a stable result leaves no marker and may resume from
the same durable evidence.

- [ ] **Step 6: Run all Worker service tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_service.py -q
```

Expected: every test passes.

Commit:

```bash
git add src/codex_mac_worker/worker.py tests/test_worker_service.py
git commit -m "feat: recover strict legacy deliveries"
```

---

### Task 6: Route retry commands durably across daemon crashes

**Files:**
- Modify: `src/codex_mac_worker/daemon.py:25-282`
- Modify: `tests/test_daemon.py:1-271`

**Interfaces:**
- Extends: `IssueProcessor.retry_delivery(repository, issue) -> str`
- Changes: `retry` calls `retry_delivery`, never `process_issue`.
- Changes: an existing unexecuted command is actionable after restart; an executed command is ignored.
- Preserves: authorization checks, resume limit, cancel behavior, and single-Worker execution.

- [ ] **Step 1: Write the failing routing test**

Extend `FakeService` with `delivery_retried: list[int]` and:

```python
def retry_delivery(self, repository: RepositoryConfig, issue: dict) -> str:
    self.delivery_retried.append(issue["number"])
    return "awaiting-review"
```

Add:

```python
def test_daemon_routes_retry_to_delivery_without_process_issue(tmp_path: Path) -> None:
    settings = config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo",
        issue_number=9,
        task_hash="hash",
        state="needs-attention",
        branch="codex/9-active",
        worktree="/tmp/worktree",
    )
    service = FakeService()

    class RetryGitHub(FakeGitHub):
        def list_comments(self, repo: str, issue_number: int) -> list[dict]:
            return [{
                "body": render_command_comment(
                    action="retry", issue_number=9, requirements=(), command_id="cmd-retry"
                ),
                "user": {"login": "owner"},
            }]

    daemon = WorkerDaemon(settings, RetryGitHub([]), store, service)

    assert daemon.process_control_commands() is True
    assert service.delivery_retried == [9]
    assert service.processed == []
    assert store.get_command("cmd-retry")["result"] == "awaiting-review"
```

- [ ] **Step 2: Write the failing crash-and-resume command test**

```python
def test_pending_retry_command_resumes_after_crash_without_comment(tmp_path: Path) -> None:
    settings = config(tmp_path)
    store = EventStore(settings.database_path)
    store.upsert_task(
        repo="owner/repo", issue_number=9, task_hash="hash", state="needs-attention",
        branch="codex/9-active", worktree="/tmp/worktree",
    )
    store.record_command("cmd-retry", "owner/repo", 9, "retry", "owner")

    service = FakeService()
    daemon = WorkerDaemon(settings, FakeGitHub([]), store, service)

    assert daemon.process_control_commands() is True
    assert service.delivery_retried == [9]
    assert store.get_command("cmd-retry")["executed_at"] is not None
```

Add a companion test using historical command ID
`503e56c5-64a7-474b-8364-299c6f929272`, mark it executed as `not-retryable`, and assert
`delivery_retried == []`. Then record a distinct pending command ID and assert only that new
command invokes `retry_delivery`.

- [ ] **Step 3: Run daemon tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_daemon.py -k 'retry' -q
```

Expected: failures because retry still calls `process_issue`, requires a generic Worker-state
flag, and cannot consume a pending ledger command without its GitHub comment.

- [ ] **Step 4: Select one authorized pending-or-new command**

Add a helper that checks `store.pending_commands(repo, issue_number)` first. Revalidate the stored
author's current repository permission before returning the oldest pending command. If none is
pending, parse comments newest-first, validate author and permission, and record the first valid
command. A duplicate command ID is usable only when `get_command(command_id)` has the exact same
repository, Issue, action, and author and `executed_at is None`; mismatched collisions are ignored.

- [ ] **Step 5: Route retry and delay command acknowledgement until stable completion**

Replace the generic Worker-state retry key check and `process_issue` call with:

```python
if command_action == "retry":
    self.store.upsert_task(
        repo=repo,
        issue_number=issue_number,
        task_hash=task["task_hash"],
        state="retrying",
        branch=task["branch"],
        worktree=task["worktree"],
    )
    result = self.service.retry_delivery(self._repository(repo), issue)
    self.store.mark_command_executed(command_id, result)
```

Do not catch `BaseException` around service execution. If the process exits between recording and
acknowledgement, `recover_active_tasks` returns `retrying` to `needs-attention`, and the pending
command is selected on the next daemon start. Resume and cancel retain their existing behavior.

- [ ] **Step 6: Run all daemon and control-state tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_daemon.py tests/test_control_state.py -q
```

Expected: all command authorization, resume, cancel, recovery, and retry tests pass.

- [ ] **Step 7: Commit daemon command durability**

```bash
git add src/codex_mac_worker/daemon.py tests/test_daemon.py
git commit -m "fix: resume delivery commands after Worker crash"
```

---

### Task 7: Document operations, verify, review, and publish

**Files:**
- Modify: `docs/OPERATIONS.md:15-55`
- Modify: `docs/MAC_MINI_SETUP.md:137-151`
- Modify: `tests/test_operational_assets.py:88-119`

**Interfaces:**
- Documents: the difference between execution retry, delivery retry, and revise.
- Documents: Issue #12 requires a new command after exact Worker deployment verification.
- Preserves: immutable PR merge approval and separate deployment approval.

- [ ] **Step 1: Write failing operator-documentation assertions**

In `test_shell_scripts_parse_and_docs_cover_manual_boundaries`, add:

```python
assert "delivery checkpoint" in operations
assert "does not rerun Codex" in operations
assert "30-minute" in operations
assert "new command ID" in operations
```

- [ ] **Step 2: Run the assertion and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_operational_assets.py::test_shell_scripts_parse_and_docs_cover_manual_boundaries -q
```

Expected: failure because checkpointed delivery recovery is not documented.

- [ ] **Step 3: Add the operator runbook**

Document that `retry` is accepted only for a checkpointed transient delivery failure or strict
legacy reconstruction, reruns approved verification but never Codex, has a 30-minute hard limit,
and requires a new command ID each time. Document that permanent validation failure requires a
new task or an independently approved Worker repair; operators must never edit SQLite or outbox.

Add the post-deployment Issue #12 sequence:

1. verify the installed commit and daemon health;
2. inspect checkpoint reconstructability through Worker/EventStore read APIs;
3. confirm retained branch, clean worktree, HEAD, sole parent, task hash, and successful matching run;
4. show the evidence and request a new explicit retry approval;
5. publish a new retry command only after approval;
6. verify the Draft PR commit equals the retained checkpoint commit.

- [ ] **Step 4: Run the complete verification suite**

Run:

```bash
git diff --check
.venv/bin/python -m pytest -q
```

Expected: no whitespace errors and the complete suite passes.

- [ ] **Step 5: Commit documentation and regression coverage**

```bash
git add docs/OPERATIONS.md docs/MAC_MINI_SETUP.md tests/test_operational_assets.py
git commit -m "docs: operate checkpointed delivery retries"
```

- [ ] **Step 6: Request independent code review**

Use `superpowers:requesting-code-review` against `origin/main...HEAD`. Resolve every Critical or
Important finding with a new failing test, minimal fix, focused test run, and commit. Re-run:

```bash
git diff --check origin/main...HEAD
.venv/bin/python -m pytest -q
```

Expected: clean diff check, complete suite green, and no unresolved Critical or Important finding.

- [ ] **Step 7: Push and open a Draft PR, then stop**

Push `codex/delivery-retry` and create a Draft PR containing the design link, checkpoint schema,
failure classification, no-Codex proof, legacy reconstruction guards, command crash behavior,
test evidence, and Mac mini deployment delta.

Show the immutable PR URL, full head SHA, changed paths, complete test result, and review findings.
Do not merge or deploy until the user explicitly approves that exact PR snapshot.

- [ ] **Step 8: Deploy only after a separate approved merge and deployment**

After explicit approval, merge the reviewed PR. On Mac mini, update from the exact merged `main`
commit, reinstall the package without replacing config, secrets, database, worktrees, or logs,
and restart `system/com.easewise.codex-worker`. Verify daemon PID/version, unchanged stderr growth,
GitHub API access, authenticated dry-run push routing, EventStore migration, and Issue #12 legacy
evidence using read-only commands. Stop and request a new retry approval; do not reuse command
`503e56c5-64a7-474b-8364-299c6f929272`.
