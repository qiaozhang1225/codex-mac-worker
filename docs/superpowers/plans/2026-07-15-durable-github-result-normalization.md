# Durable GitHub Result Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent successful GitHub label writes from crashing durable outbox confirmation when GitHub returns a list.

**Architecture:** Centralize optional remote-ID extraction in `DurableGitHub`, accepting dictionary responses with IDs and successful responses without IDs. Both immediate writes and pending flushes share the same extraction behavior.

**Tech Stack:** Python 3.12, pytest, SQLite, httpx-backed GitHub client.

## Global Constraints

- Do not edit SQLite or outbox rows manually.
- Preserve original GitHub response values from first-attempt writes.
- Preserve existing retry, task-state, and idempotency behavior.

---

### Task 1: Accept successful GitHub responses without dictionary IDs

**Files:**
- Modify: `tests/test_durable_github.py`
- Modify: `src/codex_mac_worker/durable_github.py`

**Interfaces:**
- Produces: `_remote_id(result: Any) -> str | None` used by `_write` and `flush`.

- [ ] Add failing tests for immediate and flushed `set_labels` operations returning a list.
- [ ] Run the focused tests and verify both fail with `AttributeError: 'list' object has no attribute 'get'`.
- [ ] Implement `_remote_id` and replace both direct `.get` sequences.
- [ ] Run focused tests, the complete pytest suite, and `git diff --check`.
- [ ] Commit with `fix: accept list responses in durable GitHub writes`.

### Task 2: Publish and deploy

**Files:**
- No additional source files.

- [ ] Push the isolated branch and create a Draft PR.
- [ ] Verify the PR diff and mergeability, then obtain explicit merge approval.
- [ ] Install the exact merged commit on Mac mini and kickstart the LaunchDaemon.
- [ ] Confirm the pending label outbox entry becomes delivered without database edits.
- [ ] Confirm Issue #7 progresses beyond `codex:queued` and monitor its execution.
