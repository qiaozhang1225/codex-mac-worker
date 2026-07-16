# Delivery Retry State Machine Design

## Problem

The Worker currently handles every task failure through the same `needs-attention` path.
It does not persist whether an exception is retryable, so an authorized `retry` command is
rejected even when a transient Git push error caused the failure. If retry eligibility were
stored without changing the control flow, the daemon would call `process_issue` again. That
would attempt to start the task from its original context commit, while the retained worktree
already contains the Worker-created delivery commit, and would either fail the worktree guard
or risk rerunning Codex unnecessarily.

Issue #12 demonstrates this gap: Codex execution and repository verification succeeded, the
Worker created a local delivery commit, all bounded push attempts failed, and the worktree was
retained. The required behavior is to retry delivery only. A retry must never rerun or resume
Codex, silently rebuild a commit, bypass scope checks, or duplicate a PR.

## Considered Approaches

1. **Persisted delivery checkpoint with a dedicated retry path (selected).** Save the exact
   verified commit and its evidence before the first push. A retry validates that checkpoint,
   reruns the approved repository verification, and continues with push and Draft PR creation.
   This preserves auditability and makes the operation bounded and idempotent.
2. **Replay the whole task.** Re-enter `process_issue` at the original context commit and run
   Codex again. This is rejected because it can produce different changes, spends additional
   model time, conflicts with the retained delivery commit, and violates the meaning of a
   delivery retry.
3. **Manually push the retained branch and create a PR.** This can unblock one task but bypasses
   Worker policy, durable delivery records, and reproducible recovery. It is rejected as the
   normal repair mechanism.

## State and Persistence

Add a `delivery_checkpoints` table keyed by repository, Issue number, and frozen task hash. A
checkpoint contains:

- branch and worktree path;
- delivery commit SHA and context commit SHA;
- serialized structured Codex result;
- model, Codex CLI version, and session ID;
- project configuration hash, verification profile, commands, result summary, and completion
  timestamp;
- creation and update timestamps.

The Worker writes the checkpoint only after Codex has finished, the scope and repository guards
have passed, the approved verification profile has passed, and the Worker has created the
delivery commit. The write must commit before the first push attempt.

Retry eligibility is derived from both the checkpoint and the classified delivery error; it is
not a generic task flag. A transient push or Draft PR transport failure leaves the task in
`needs-attention` with delivery retry eligibility. Authentication failure, authorization denial,
policy failure, validation drift, verification failure, or any other permanent error clears or
withholds eligibility. Status evidence identifies the failed delivery phase without exposing
credentials.

## Normal Delivery Flow

The normal execution flow remains bounded:

1. freeze and validate the task;
2. prepare the isolated worktree and run Codex once;
3. validate HEAD, changed paths, size limits, protected paths, secrets, and binary files;
4. run the repository-approved verification profile;
5. create the Worker delivery commit;
6. persist the delivery checkpoint;
7. push that exact commit and create or reconcile the Draft PR;
8. transition to `awaiting-review`.

Failures before step 6 follow the existing execution and verification policies and cannot use
delivery retry. Failures in steps 7 or 8 are classified. Only transient delivery failures become
eligible for the dedicated retry path.

## Dedicated Retry Flow

Add `WorkerService.retry_delivery(repository, issue)` and expose it through the daemon's issue
processor interface. An authorized `retry` command for a `needs-attention` task calls this method
instead of `process_issue`. It performs no Codex runner call and accepts no resume session ID.

Before any network write, it must prove all of the following:

- the current Issue still contains the same frozen task block and task hash;
- the repository remains bound to the trusted GitHub App and its project policy is valid;
- the stored worktree exists on the stored task branch and is clean;
- worktree HEAD exactly equals the checkpoint delivery commit;
- the delivery commit has the frozen context commit as its sole parent;
- the recomputed diff still passes allowed-path, protected-path, file-count, line-count, secret,
  binary-file, and HEAD guards;
- the retained worktree's project configuration hash, verification profile, and commands exactly
  equal the checkpoint values;
- the checkpoint-recorded approved verification commands pass again within their existing
  bounded timeout.

If any proof fails, the Worker stops in `needs-attention`, removes delivery retry eligibility,
and records a permanent validation result. It never resets the worktree, creates a replacement
commit, or invokes Codex to repair the changes.

After validation, the Worker pushes the existing commit with its normal temporary GitHub App
credentials and bounded Git routing. It then creates the Draft PR through `DurableGitHub`. The
existing head-branch reconciliation is retained so a crash or ambiguous API response cannot
create a duplicate PR. Success stores the PR number, clears retry eligibility, and transitions
the task to `awaiting-review`.

The entire delivery retry, including validation, verification, push, and PR reconciliation, has
a fresh 30-minute hard timeout. Heartbeats, Git retries, and remote API responses cannot extend
that deadline.

If a new transient delivery error occurs, the same checkpoint remains eligible for one later,
separately approved retry command. Existing task hard deadlines and automatic attempt limits do
not authorize automatic delivery retries; every retry remains an explicit user action.

## Legacy Recovery for Issue #12

Issue #12 predates delivery checkpoints. The first deployment may reconstruct one legacy
checkpoint, but only from strict, local evidence:

- the recorded worktree and branch still exist and the worktree is clean;
- HEAD is a single delivery commit whose sole parent is the frozen context commit;
- the task row and frozen Issue still contain the same task hash, and EventStore contains a
  completed successful run whose session ID equals the task's recorded session ID;
- the stored final message parses against the current frozen result schema;
- the recomputed diff passes every current repository scope guard;
- the current approved verification profile passes again.

When all conditions pass, the Worker persists a normal checkpoint before making a network write.
If any condition is absent or ambiguous, reconstruction is rejected and the Worker does not
guess. Legacy reconstruction is scoped to tasks that already retain this complete evidence; it
does not weaken checkpoint requirements for new tasks.

The previously consumed retry command for Issue #12 remains executed with its historical
`not-retryable` result. Deployment of this change does not replay it. A new command ID and new
explicit approval are required after the Worker reports that reconstruction is available.

## Command Crash Recovery and Idempotency

The command ledger must distinguish a newly seen command from an existing unexecuted command.
On restart, a recorded command with no `executed_at` remains actionable; an executed command is
never run twice. The daemon marks a command executed only after the retry reaches a stable state:
`awaiting-review`, a classified `needs-attention`, or `cancelled`.

A crash between push and PR creation is safe because the checkpoint fixes the commit, the push is
idempotent for the same branch and SHA, and Draft PR creation reconciles by head branch. A crash
after a remote write but before local acknowledgement is handled by the existing durable outbox
and remote reconciliation rather than by repeating model execution.

## Security and Scope

The checkpoint contains execution evidence but no GitHub App token, private key, Askpass value,
or deployment credential. Codex continues to receive none of those credentials. Retry does not
grant merge, deployment, workflow, production-data, or high-risk permissions. The Worker still
never calls the merge API.

No operator repair may directly edit or delete SQLite rows or outbox entries. Migration and state
changes are performed only through EventStore methods and tested schema migrations.

## Validation

Automated tests will prove:

- a transient push or Draft PR transport failure persists a checkpoint and delivery retry
  eligibility;
- a permanent delivery failure does not become retryable;
- a delivery retry never invokes, resumes, or reconstructs a Codex run;
- a retry pushes the checkpoint commit and reaches `awaiting-review` after Draft PR creation;
- an existing remote branch or PR is reconciled without duplication;
- task-hash, branch, HEAD, parent, worktree, diff, configuration, or verification drift rejects
  the retry before a network write;
- strict legacy reconstruction accepts the retained Issue #12 evidence shape and rejects every
  incomplete or ambiguous variant;
- a pending command is resumed after daemon restart while an executed command remains idempotent;
- a crash at each delivery boundary converges through checkpoint and outbox reconciliation;
- the full Worker test suite passes.

After deployment, the Mac mini will be checked for the expected Worker version, healthy daemon,
clean retained Issue #12 worktree, and reconstructable checkpoint evidence. Only then will a new
Issue #12 retry command be proposed for explicit approval.
