# Post-Execution Validation Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover EaseWise Issue #19 from its retained successful Codex run without rerunning Codex, while allowing unchanged baseline secret-like text.

**Architecture:** Secret scanning subtracts matches already present at the exact diff baseline. `WorkerService` persists successful session evidence before integrity checks, reconstructs one strict retained execution, and reuses a verified-delivery finalizer after one repository-owned verification run. `WorkerDaemon` routes only this pre-commit state to the new path.

**Tech Stack:** Python 3.12, SQLite, Git, pytest, GitHub App API, macOS `launchd`, Codex CLI.

## Global Constraints

- Post-execution recovery never invokes or resumes Codex.
- Never log matched credential values, tokens, App keys, or Askpass values.
- Require immutable task hash, retained branch/worktree, unchanged context HEAD, no PR/checkpoint, and one unambiguous successful run.
- Use only repository-owned verification commands and a 30-minute bounded retry.
- Keep existing integration-refresh limits and idempotent delivery.
- Do not cancel or reissue Issue #19.
- Production operations, production data, high risk, Goal mode, and automatic rollback remain excluded.

---

### Task 1: Baseline-aware secret matching

**Files:**
- Modify: `src/codex_mac_worker/verification.py:35-66`
- Modify: `src/codex_mac_worker/worker.py:505-532,1518-1532`
- Test: `tests/test_prompting_verification.py`

**Interfaces:**
- Consumes: the validated Git commit SHA used as the delivery diff baseline.
- Produces: `scan_for_secrets(worktree: Path, changed_paths: list[str] | tuple[str, ...], *, baseline_ref: str | None = None, max_binary_bytes: int = 1_000_000) -> None`.

- [ ] **Step 1: Write the EaseWise SQL false-positive test**

Add `subprocess`, a small `git(cwd, *args)` helper, and this real-repository scenario:

```python
def test_secret_scanner_ignores_match_unchanged_from_baseline(tmp_path: Path) -> None:
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    target = tmp_path / "database.py"
    target.write_text(
        "query = \"\"\"UPDATE keys SET secret_ref = "
        "'admin:aliyun:bailian_api_key:' || id\\n"
        "WHERE provider = 'aliyun'\"\"\"\\nvalue = 1\\n",
        encoding="utf-8",
    )
    git(tmp_path, "add", "database.py")
    git(tmp_path, "commit", "-m", "baseline")
    baseline = git(tmp_path, "rev-parse", "HEAD")
    target.write_text(target.read_text().replace("value = 1", "value = 2"))
    scan_for_secrets(tmp_path, ["database.py"], baseline_ref=baseline)
```

- [ ] **Step 2: Run RED**

```bash
PYTHONPATH="$PWD/src" /Users/qiaoz-macair/Documents/codex-mac-worker/.venv/bin/python -m pytest -q --tb=short tests/test_prompting_verification.py::test_secret_scanner_ignores_match_unchanged_from_baseline
```

Expected: FAIL because `baseline_ref` is unsupported.

- [ ] **Step 3: Add new-secret rejection cases**

Parameterize clean baselines changed to password, JSON `api_key`, private key, GitHub token, AWS key, and Aliyun key content. Add a changed-value case where the baseline and current file both match but have different quoted credential values. Every case must raise filename-only `VerificationError`.

- [ ] **Step 4: Implement match multiset subtraction**

Add:

```python
from collections import Counter

def _secret_matches(text: str) -> Counter[str]:
    return Counter(
        match.group(0)
        for pattern in _SECRET_PATTERNS
        for match in pattern.finditer(text)
    )
```

Use `git show <baseline>:<path>` without a shell to read baseline text. Verify the commit separately with `git cat-file -e`; an unavailable commit raises `VerificationError`, while an absent path means an empty baseline. Subtract the baseline counter from current matches and raise only when a positive current count remains. Keep whole-file binary size enforcement. Callers without `baseline_ref` retain conservative whole-file scanning.

- [ ] **Step 5: Pass exact baselines from Worker guards**

In `_validate_delivery_diff` and `_validate_committed_delivery`, pass their `baseline_head`. In `_retry_delivery_bounded`, pass `integrated_base_sha`.

- [ ] **Step 6: Run GREEN and commit**

```bash
PYTHONPATH="$PWD/src" /Users/qiaoz-macair/Documents/codex-mac-worker/.venv/bin/python -m pytest -q --tb=short tests/test_prompting_verification.py tests/test_worker_service.py
git add src/codex_mac_worker/verification.py src/codex_mac_worker/worker.py tests/test_prompting_verification.py
git commit -m "fix: compare secret matches with task baseline"
```

---

### Task 2: Persist and reconstruct successful execution evidence

**Files:**
- Modify: `src/codex_mac_worker/worker.py:1070-1165`
- Test: `tests/test_worker_service.py`

**Interfaces:**
- Consumes: `EventStore.list_runs(repo, issue_number)` and the retained task row.
- Produces: `_successful_execution_result(repo, issue_number, task) -> RunnerResult` and a durable task session before integrity checks.

- [ ] **Step 1: Write the persistence failure test**

Force `_validate_delivery_diff` to raise after `FakeRunner` succeeds, then assert the terminal task keeps `worktree` and `session_id == "session-1"` while entering `needs-attention`.

- [ ] **Step 2: Run RED**

Expected: the task currently has `session_id is None`.

- [ ] **Step 3: Persist session before structured-result and diff checks**

Immediately after `finish_run`, when exit code is zero, termination is absent, and session ID is non-empty, call `upsert_task` with state `running`, branch, worktree, and session ID. Do not change pause, cancel, or nonzero-run behavior.

- [ ] **Step 4: Write strict reconstruction tests**

Create `prepare_post_execution_failure(tmp_path, damage=None)` that leaves a dirty task worktree at the context HEAD and records the `FakeRunner` result. Verify one successful run is accepted. Parameterize missing, failed, terminated, ambiguous, and session-mismatched runs as `PolicyError` cases.

- [ ] **Step 5: Run RED for the missing helper**

Expected: FAIL because `_successful_execution_result` does not exist.

- [ ] **Step 6: Implement strict reconstruction**

Select finished exit-zero runs with result dictionaries, no termination reason, non-empty session IDs, and non-empty final messages. If the task has a session ID, require exactly one matching candidate. Without one, permit exactly one candidate for the legacy Issue #19 shape. Rebuild `RunnerResult`; reject zero or multiple candidates.

- [ ] **Step 7: Run GREEN and commit**

```bash
PYTHONPATH="$PWD/src" /Users/qiaoz-macair/Documents/codex-mac-worker/.venv/bin/python -m pytest -q --tb=short tests/test_worker_service.py
git add src/codex_mac_worker/worker.py tests/test_worker_service.py
git commit -m "fix: persist successful execution evidence"
```

---

### Task 3: Finalize retained execution without Codex

**Files:**
- Modify: `src/codex_mac_worker/worker.py:1170-1321,1390-1710`
- Test: `tests/test_worker_service.py`

**Interfaces:**
- Consumes: `_successful_execution_result`, baseline-aware delivery guards, existing Git integration and checkpoint delivery.
- Produces: `retry_execution_delivery(repository: RepositoryConfig, issue: dict[str, Any]) -> str` and the keyword-only `_finalize_verified_execution` method returning `None` with the exact inputs listed in Task 3 Step 3.

- [ ] **Step 1: Write the no-Codex recovery test**

Use `prepare_post_execution_failure`, replace `service.runner` with a `MustNotRun` object whose `run` raises `AssertionError`, call `retry_execution_delivery`, then assert `awaiting-review`, PR `44`, one unchanged run, and a normal delivery checkpoint.

- [ ] **Step 2: Run RED**

Expected: FAIL because `retry_execution_delivery` does not exist.

- [ ] **Step 3: Extract the verified finalizer**

Create keyword-only `_finalize_verified_execution` consuming repository, Issue, spec, task hash, branch, worktree, mirror, baseline head, project config, `RunnerResult`, structured result, `VerificationResult`, hard deadline, command monitor, and status-comment ID. Move the existing final Issue-hash check, commit, main refresh/integration, post-integration checks, optional re-verification, checkpoint save, and `_deliver_checkpoint` call into it without changing order or fields. Replace normal flow with this helper.

```python
def _finalize_verified_execution(
    self,
    *,
    repository: RepositoryConfig,
    issue: dict[str, Any],
    spec: Any,
    task_hash: str,
    branch: str,
    worktree: Path,
    mirror: Path,
    baseline_head: str,
    project_config: ProjectConfig,
    runner_result: RunnerResult,
    structured_result: dict[str, Any],
    verification_result: VerificationResult,
    hard_deadline: datetime,
    monitor: Callable[[], str | None],
    status_comment_id: int,
) -> None:
```

- [ ] **Step 4: Implement bounded recovery preflight**

`retry_execution_delivery` uses `DELIVERY_RETRY_TIMEOUT_SECONDS`. Require matching task/Issue, no PR/checkpoint, retained branch/worktree, current branch match, HEAD equal to context commit, and a non-empty dirty diff. Reconstruct and schema-check the successful result, reload trusted project policy/context, and run `_validate_delivery_diff` before any remote write.

- [ ] **Step 5: Verify once and finalize**

Set state/comment to `retrying` with detail `post-execution validation recovery`. Run the repository verification profile once; never perform automatic model repair. On success invoke `_finalize_verified_execution` and return the durable task state (`awaiting-review` or `merging`). On error use `_mark_attention`, retain evidence, and return `needs-attention`.

- [ ] **Step 6: Add fail-closed tests**

Cover task-hash, branch, HEAD, protected-path, new-secret, missing-worktree, failed-verification, and overlapping-main failures. Assert no PR, no additional run, and no remote task branch. Add a non-overlapping advanced-main success case with a two-parent integration commit and two verification calls.

- [ ] **Step 7: Run GREEN and commit**

```bash
PYTHONPATH="$PWD/src" /Users/qiaoz-macair/Documents/codex-mac-worker/.venv/bin/python -m pytest -q --tb=short tests/test_worker_service.py
git add src/codex_mac_worker/worker.py tests/test_worker_service.py
git commit -m "feat: recover successful pre-commit executions"
```

---

### Task 4: Route retry by durable phase

**Files:**
- Modify: `src/codex_mac_worker/daemon.py:45-78,310-385`
- Modify: `tests/test_daemon.py:52-100,259-330`

**Interfaces:**
- Consumes: task row, delivery checkpoint, and `EventStore.list_runs`.
- Produces: `IssueProcessor.retry_execution_delivery(repository, issue) -> str` routing before delivery retry.

- [ ] **Step 1: Add `FakeService.execution_retried` and Issue #19 routing test**

The test task has `needs-attention`, branch, worktree, no PR/checkpoint/session, and one finished successful run. Submit an authorized retry and assert execution recovery is called once, delivery/process paths are untouched, and the command result is `awaiting-review`.

- [ ] **Step 2: Run RED**

Expected: the daemon calls `retry_delivery`.

- [ ] **Step 3: Implement phase predicate and route**

After completed automatic delivery and before pre-execution retry, identify: checkpoint absent, worktree present, PR absent, and at least one finished exit-zero run whose result has no termination reason and has a session ID. Call `retry_execution_delivery`, then mark the command with its stable result. Keep no-run/no-worktree pre-execution routing and committed-delivery routing unchanged.

- [ ] **Step 4: Add negative cases and run GREEN**

Prove failed/terminated runs do not use execution recovery, pre-execution evidence still calls normal processing, and PR/checkpoint evidence still calls delivery retry.

```bash
PYTHONPATH="$PWD/src" /Users/qiaoz-macair/Documents/codex-mac-worker/.venv/bin/python -m pytest -q --tb=short tests/test_daemon.py
git add src/codex_mac_worker/daemon.py tests/test_daemon.py
git commit -m "fix: route successful execution recovery"
```

---

### Task 5: Verify, publish, deploy, and recover Issue #19

**Files:**
- Verify: all intended source, tests, design, and plan files
- Deploy source: `/Users/qiaoz-macmini/codex-mac-worker`
- Install: `/Users/qiaoz-macmini/Library/Application Support/CodexWorker/venv`
- Observe: `/Users/qiaoz-macmini/Library/Application Support/CodexWorker/worktrees/qiaozhang1225/EaseWise/19-task`

**Interfaces:**
- Consumes: merged Worker commit and existing Issue #19 evidence.
- Produces: healthy installed Worker and recovered Issue #19 delivery with run count still `1`.

- [ ] **Step 1: Run full verification**

```bash
PYTHONPATH="$PWD/src" /Users/qiaoz-macair/Documents/codex-mac-worker/.venv/bin/python -m pytest -q --tb=short
git diff --check
git status --short
```

- [ ] **Step 2: Review security and scope**

Confirm there is no Goal mode, deployment, production-data access, repository-specific allowlist, new Codex call, unbounded retry, credential logging, or unrelated refactor.

- [ ] **Step 3: Push, create PR, verify exact head, and squash merge**

Push `codex/fix-post-execution-recovery`; create PR title `Recover successful pre-commit Worker executions`. The PR body records the Issue #19 false positive, no-Codex guarantee, test command, and rollout gate. Merge only the inspected exact head after checks pass or GitHub reports no configured checks.

- [ ] **Step 4: Install merged Worker on Mac mini**

Fast-forward `/Users/qiaoz-macmini/codex-mac-worker`, force-reinstall it into the Worker venv, stop the old user-owned PID, and run `launchctl kickstart system/com.easewise.codex-worker`. Verify a new PID, merged source HEAD, and installed `baseline_ref` plus `retry_execution_delivery` markers.

- [ ] **Step 5: Prove Issue #19 evidence before mutation**

Read-only checks confirm `needs-attention`, unchanged task hash, retained `codex/19-task`, dirty worktree at `088d995dff3297f9b4030641c038595e5e45ede5`, no PR/checkpoint, exactly one successful run, and baseline-aware scanner acceptance.

- [ ] **Step 6: Submit one retry and monitor without Codex**

Run `codexctl task retry https://github.com/qiaozhang1225/EaseWise/issues/19`. Monitor `verifying`, Draft PR/`merging`, and `completed` or evidence-backed attention. At every state confirm run count remains `1` and no `codex exec` child starts.

- [ ] **Step 7: Final live verification and cleanup**

Verify daemon health, Issue/PR/merge linkage, checkpoint evidence, tests, and unchanged run count. Preserve the user's unrelated EaseWise modification `docs/operations/production-cloud-resource-inventory.md`. Remove only the merged implementation worktree/branch and temporary files.
