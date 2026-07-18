# Post-Execution Validation Recovery Design

## Problem

EaseWise Issue #19 completed one bounded Codex run with exit code `0` and left the intended
changes in its retained task worktree. Before the Worker could enter repository verification,
the secret scanner rejected `product/backend/api/database.py`. The match was not introduced by
the task: the frozen context already contained this static SQL value:

```text
secret_ref = 'admin:aliyun:bailian_api_key:' || id
```

The generic credential regular expression interpreted the embedded `api_key:` suffix, the SQL
quote, and a later quote after `WHERE provider =` as one credential assignment. The scanner
examines the full content of every changed file, so an unrelated edit made this pre-existing
false positive blocking.

The failure also exposes a recovery gap. The successful run is durable in `runs`, and the dirty
worktree is retained, but the task session is currently persisted only after the first delivery
diff check. No delivery checkpoint exists before repository verification and commit creation.
Consequently, the existing `retry` path treats this state as an incomplete legacy delivery and
cannot continue without either rerunning Codex or manually editing Worker state.

## Considered Approaches

1. **Baseline-aware scanning plus bounded post-execution recovery (selected).** Compare current
   secret matches with the frozen baseline and reject only matches newly introduced by the task.
   Persist successful execution identity before validation, and allow an explicit retry to
   re-run deterministic guards, approved verification, commit, integration, and delivery without
   invoking Codex.
2. **Repository-specific scanner allowlist.** Add the EaseWise SQL fragment or
   `database.py` to an exclusion. This is rejected because it weakens security for one target
   repository and would hide future real credentials in the same file.
3. **Cancel and recreate Issue #19.** Fix only the scanner, discard the retained worktree, and
   run Codex again. This is safe but wastes model execution and dependency preparation, and it
   leaves the same post-execution recovery gap for the next policy or verification defect.

## Baseline-Aware Secret Scanning

The scanner continues to recognize private keys, GitHub tokens, cloud access keys, and quoted
credential assignments. Worker delivery checks additionally provide the exact baseline commit
used for the diff.

For every changed text file, the scanner computes the credential-pattern matches in both the
current file and the file at that baseline commit. It compares match values as a multiset and
rejects every current occurrence that is not already present in the baseline. A new file has an
empty baseline. A changed credential value is therefore new and rejected; an unchanged
pre-existing match is not attributed to the task. Match values and file contents are never
written to status comments or logs.

Binary size enforcement remains whole-file based. Callers without a baseline retain the current
whole-file behavior so standalone scanner tests and conservative uses do not silently weaken.

This design intentionally does not declare a pre-existing plaintext credential safe. Repository
history and CI remain responsible for secrets that already exist on the frozen base. The Worker
gate proves that the delegated task did not introduce an additional credential.

## Successful Execution Evidence

Immediately after a Codex process exits successfully, the Worker already stores its run record.
Before parsing the structured result or scanning the diff, it will also persist the task's
worktree and session ID while retaining state `running`. This creates an unambiguous link among:

- repository, Issue number, and frozen task hash;
- task branch and retained worktree;
- context commit and unchanged task HEAD;
- successful run, session ID, structured final message, model, and CLI version.

No new model output or credential is stored. For Issue #19, which predates this ordering change,
legacy reconstruction is permitted only because it has exactly one finished successful run, no
termination reason, no PR, no delivery checkpoint, a retained worktree, and an unchanged context
HEAD. Multiple candidate runs or missing evidence fail closed.

## Post-Execution Retry Flow

An authorized `retry` command for a `needs-attention` task is routed to the new post-execution
path only when there is no delivery checkpoint or PR, a retained worktree exists, and a
successful run candidate exists. Pre-execution retry and committed-delivery retry retain their
existing routes.

The post-execution path performs no `codex exec` or session resume. Within a fresh bounded
30-minute deadline it must:

1. revalidate the Issue author, immutable task hash, repository authority, trusted App binding,
   project policy, context files, branch, worktree, and unchanged context HEAD;
2. reconstruct and schema-validate the successful structured Codex result from the durable run;
3. recompute the diff and apply allowed-path, protected-path, file-count, line-count, binary, and
   baseline-aware secret checks;
4. run the repository-owned verification profile once, with no automatic model repair;
5. create the Worker task commit, refresh and integrate the current default branch using the
   existing conflict and refresh limits, and revalidate/reverify when integration advances;
6. persist the normal delivery checkpoint before the first push;
7. continue through the existing idempotent push, Draft PR, automatic merge, and reconciliation
   flow.

If any proof or approved test fails, the task returns to `needs-attention`; the worktree and run
evidence remain unchanged. A retry command is marked executed only after this attempt reaches a
stable state. Repeating a command ID never repeats work.

## State Routing

The daemon distinguishes three retry shapes in order:

1. no worktree, no session, and no runs: pre-execution retry through normal issue processing;
2. no delivery checkpoint or PR, retained worktree, and successful run evidence:
   post-execution validation recovery;
3. persisted or strictly reconstructable committed delivery evidence: delivery retry.

This routing preserves the rule that an infrastructure retry cannot silently rerun Codex after
a successful model execution.

## Validation

Automated tests must prove:

- the EaseWise SQL fragment matches the generic scanner but is accepted when unchanged from the
  baseline;
- a newly added or changed credential, private key, GitHub token, AWS key, or Aliyun key is still
  rejected;
- successful execution identity is persisted before delivery diff validation;
- the daemon routes Issue #19-shaped evidence to post-execution recovery rather than legacy
  delivery retry;
- recovery accepts one exact successful run and rejects missing, failed, terminated, or ambiguous
  runs;
- recovery never calls or resumes the Codex runner;
- scope, branch, HEAD, task hash, project configuration, secret, binary, verification, or
  integration drift stops before push;
- a valid recovery creates the normal checkpoint and reaches the existing delivery/automatic
  merge flow without a duplicate commit, branch, PR, or command execution;
- existing pre-execution, delivery retry, manual merge, and automatic merge tests remain green;
- the full Worker test suite passes before deployment.

## Rollout

The implementation is merged into `qiaozhang1225/codex-mac-worker`, installed on the Mac mini,
and the LaunchDaemon is restarted. Installed source markers, PID, full tests, Issue #19 retained
worktree, successful run identity, and baseline-aware scanner result are checked before a new
authorized retry command is submitted.

Issue #19 is not cancelled or reissued. Its existing Codex result is reused, and only Worker
validation, repository verification, commit, integration, delivery, and automatic merge are
allowed to continue. EaseWise production deployment and production data remain out of scope.
