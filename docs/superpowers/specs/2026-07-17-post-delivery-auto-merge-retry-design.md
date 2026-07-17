# Post-Delivery Automatic Merge Retry Design

## Context

An automatic merge policy blocker can move a task to `needs-attention` after
the Worker has already created a verified delivery checkpoint and Draft PR.
The current `retry` command always calls `retry_delivery`, which correctly
rejects a completed, non-retryable delivery checkpoint. This prevents the
operator from asking the Worker to re-evaluate a corrected external merge
policy without rerunning Codex or editing SQLite.

## Chosen behavior

In local `merge_mode = "automatic"`, an authorized `retry` command re-arms
automatic merge when all of the following durable facts are present:

- the task is in `needs-attention`;
- the task records an integer PR number;
- the matching delivery checkpoint exists;
- the checkpoint phase is `complete` and `retryable` is false.

The daemon changes the task state to `merging`, updates the remote lifecycle
label, and records the command result as `merging`. It does not call
`retry_delivery`, `process_issue`, or the Codex runner. The normal automatic
merge loop then re-fetches main, reconciles the exact PR head, refreshes and
re-verifies an advanced non-overlapping main when necessary, checks the
automatic Ruleset and all merge gates, and performs the existing durable
squash-merge operation.

All other retry cases retain the existing delivery-retry behavior. Manual
mode, missing PRs, absent checkpoints, incomplete checkpoints, and retryable
delivery failures are not reclassified as automatic merge retries.

## Crash and idempotency behavior

The task is persisted as `merging` before the command is acknowledged. A
crash after that state change is safe because startup and the normal review
loop already reconcile `merging` tasks. If the command remains pending after
the merge, stable-command reconciliation acknowledges it from the completed
task state. Repeated command IDs remain idempotent through the existing
commands table.

## Verification

Regression coverage must prove that a completed automatic delivery retry:

- transitions to `merging`;
- updates the remote lifecycle label to `codex:merging`;
- records command result `merging`;
- never calls delivery retry or Codex execution;
- is subsequently consumed by the existing automatic merge loop.

Existing tests must continue to prove that ordinary retryable delivery
failures still call `retry_delivery` and that manual mode does not adopt the
automatic path.
