# Post-Delivery Automatic Merge Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow an authorized retry command to re-arm a completed automatic delivery for merge-policy re-evaluation without rerunning Codex.

**Architecture:** Add a narrow predicate in `WorkerDaemon.process_control_commands` for `needs-attention` tasks that have an integer PR and a completed non-retryable delivery checkpoint while local merge mode is automatic. Persist `merging`, publish the lifecycle label, and acknowledge the command; leave every other retry on the existing delivery-retry path.

**Tech Stack:** Python 3.12, SQLite-backed `EventStore`, pytest

## Global Constraints

- Never invoke Codex for a completed delivery checkpoint retry.
- Never edit Worker SQLite manually.
- Preserve existing delivery retry, manual merge, exact-head, Ruleset, scope, and verification gates.
- Do not add a new command or change the Issue protocol.

---

### Task 1: Re-arm completed automatic deliveries

**Files:**
- Modify: `src/codex_mac_worker/daemon.py`
- Modify: `src/codex_mac_worker/store.py`
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `EventStore.get_delivery_checkpoint(repo, issue_number, task_hash) -> dict[str, Any] | None`
- Consumes: `EventStore.has_executed_command_result(repo, issue_number, results) -> bool`
- Produces: `WorkerDaemon.process_control_commands() -> bool` routes an eligible `retry` to durable state `merging` without calling `WorkerService.retry_delivery`

- [ ] **Step 1: Write the failing regression test**

Add a daemon test that stores a `needs-attention` task with PR `44`, saves a
delivery checkpoint, changes it to `phase="complete"` and `retryable=False`,
records an authorized retry command, and uses a fake GitHub client that records
lifecycle label writes:

```python
class LabelGitHub(FakeGitHub):
    def __init__(self) -> None:
        super().__init__([])
        self.label_updates: list[list[str]] = []

    def set_labels(
        self, repo: str, issue_number: int, labels: list[str]
    ) -> dict:
        self.label_updates.append(labels)
        return {"labels": labels}

assert daemon.process_control_commands() is True
assert store.get_task("owner/repo", 9)["state"] == "merging"
assert store.get_command("cmd-auto-retry")["result"] == "merging"
assert service.delivery_retried == []
assert service.processed == []
assert github.label_updates[-1] == ["codex:merging"]
```

- [ ] **Step 2: Run the focused test and verify red**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_daemon.py -k completed_automatic_delivery
```

Expected: FAIL because the daemon calls `retry_delivery` and returns
`awaiting-review` instead of re-arming `merging`.

- [ ] **Step 3: Implement the minimal routing predicate**

Inside the existing `action == "retry"` branch, load the matching checkpoint
and use this exact eligibility rule:

```python
completed_automatic_delivery = (
    self.config.merge_mode == "automatic"
    and isinstance(task.get("pr_number"), int)
    and not isinstance(task.get("pr_number"), bool)
    and checkpoint is not None
    and checkpoint.get("phase") == "complete"
and checkpoint.get("retryable") is False
)
```

For the one legacy state produced by the old retry path, also accept
`phase="validation"` only when `last_error` exactly equals
`PolicyError: delivery checkpoint is not retryable` and an earlier executed
command result is `awaiting-review` or `merging`. Add a negative assertion that
the same validation checkpoint without prior success evidence still calls
`retry_delivery`.

When true, upsert the task as `merging` while preserving branch, worktree,
session ID, and PR number; call `_set_remote_state(..., "merging")`; mark the
command result `merging`; return `True`. Otherwise execute the unchanged
delivery retry branch.

- [ ] **Step 4: Run focused daemon tests and verify green**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_daemon.py
```

Expected: PASS with zero failures.

- [ ] **Step 5: Run the full suite and compile check**

Run:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m compileall -q src tests
```

Expected: both commands exit `0`.

- [ ] **Step 6: Commit the focused change**

```bash
git add docs/superpowers/specs/2026-07-17-post-delivery-auto-merge-retry-design.md \
  docs/superpowers/plans/2026-07-17-post-delivery-auto-merge-retry.md \
  src/codex_mac_worker/daemon.py src/codex_mac_worker/store.py tests/test_daemon.py
git commit -m "Fix automatic merge policy retries"
```

### Task 2: Publish, deploy, and recover EaseWise Issue #12

**Files:**
- No source changes

**Interfaces:**
- Consumes: merged `codex-mac-worker` main, Mac mini LaunchDaemon, `codexctl task retry`
- Produces: EaseWise PR #13 merged and Issue #12 completed with exactly one Codex run

- [ ] **Step 1: Publish the reviewed branch and merge its exact PR head**

Push `codex/auto-merge-policy-retry`, create a PR, verify its exact head and
checks, then merge only that reviewed head.

- [ ] **Step 2: Upgrade the Mac mini Worker**

Fast-forward `/Users/qiaoz-macmini/codex-mac-worker`, force-reinstall the
package into the Worker venv, restart `system/com.easewise.codex-worker`, and
verify the new PID and installed commit.

- [ ] **Step 3: Submit one new infrastructure retry**

Run:

```bash
codexctl task retry https://github.com/qiaozhang1225/EaseWise/issues/12
```

Expected: the command is recorded once and the task transitions through
`merging` without creating another Codex run.

- [ ] **Step 4: Verify the final durable outcome**

Confirm PR #13 is merged at its expected head, Issue #12 is closed with
`codex:completed`, `runs` remains `1`, exactly one completed auto-merge
operation exists, and no duplicate PR was created.
