# Dual Mac Single-Owner Auto-Merge Design

## 1. Purpose

This design belongs to the dual-Mac Worker system implemented in `qiaozhang1225/codex-mac-worker`. EaseWise is the first target repository and rollout case; its product code does not own this architecture.

The system optimizes one person's development throughput across two Macs:

- the MacBook Codex agent is the principal development agent;
- the MacBook may implement work directly or decompose and delegate bounded subtasks;
- the always-on Mac mini Worker is a subordinate execution agent;
- a verified Worker delivery enters `main` automatically so the owner can judge the result in the test environment;
- human approval is not simulated where no independent human reviewer exists.

Codex Goal mode, Codex cloud execution, production deployment, production-data access, high-risk work, and automatic rollback remain excluded.

## 2. Roles and Authority

### Human owner

The owner selects product direction, authorizes a parent development objective, observes the test environment, and decides whether a bad merge should be reverted. Production and irreversible operations still require a separate explicit authorization.

### MacBook principal agent

The MacBook Codex agent is both a full developer and the delegation controller. Within an already authorized parent objective it may:

- investigate, design, edit, test, commit, and publish work itself;
- decide whether the work benefits from Mac mini execution;
- split the objective into independently verifiable low- or medium-risk subtasks;
- dispatch those subtasks without asking the owner to reconfirm every internal delegation decision;
- continue non-conflicting development while the Worker runs.

Delegation does not expand authority. A subtask must remain inside the parent objective and repository permissions. New product scope, high risk, production operations, deployment, credentials, or irreversible work still stop for owner direction.

### Mac mini Worker

The Worker executes the frozen task contract. It may not reinterpret the parent objective, split work again, create follow-on tasks, widen `allowed_paths`, select new verification commands, deploy, access production data, or invoke Codex Goal mode.

The Worker owns mechanical execution decisions: worktree preparation, Codex invocation, bounded retry, verification, integration refresh, Draft PR creation, exact-head auto-merge, and crash reconciliation.

## 3. Delegation Contract

A Worker task continues to use an immutable GitHub Issue block containing the context commit, one objective, acceptance criteria, context files, allowed paths, verification profile, and low/medium risk.

The MacBook dispatch workflow changes in one respect: `codexctl task create --yes` is permitted without another human prompt when all of the following are true:

1. the current user request already authorizes the parent development objective;
2. the subtask is a strict subset of that objective;
3. the task passes repository policy and conflict checks;
4. no new external side effect or permission is introduced.

An explicitly requested standalone dispatch still shows the final task specification before creation. The audit record always includes the parent context, frozen task hash, context commit, dispatching GitHub identity, and resulting Issue URL.

## 4. Concurrent Development and Path Ownership

MacBook and Mac mini work only on branches or isolated worktrees, never by editing shared `main` in place.

Before dispatch, the MacBook agent reads all nonterminal Worker Issues in the repository and compares their `allowed_paths` with:

- the proposed subtask paths;
- files already changed in the MacBook worktree;
- paths planned for the MacBook's remaining direct work.

Prefix overlap, renamed-path overlap, or an unknown modification scope blocks concurrent dispatch. The agent either keeps the work locally, narrows the task, or sequences it after the existing Worker task. The Worker also rejects a newly queued task whose allowed paths overlap another active task, preventing two delegated tasks from claiming the same scope.

The first implementation uses GitHub Issue state as the shared ownership ledger; it does not introduce a central coordination repository or a second network service.

## 5. Trusted Auto-Merge Opt-In

Automatic merge requires two independent trusted signals:

1. the Mac mini's local `worker.toml` sets `merge_mode = "automatic"`;
2. the target repository has the recognized single-owner Ruleset profile.

Repository source code cannot enable auto-merge. The local config is outside task worktrees, and changing the Ruleset requires repository administration.

`merge_mode` defaults to `"manual"` for existing installations. Manual mode preserves the current `awaiting-review` and `codexctl task merge` workflow.

### Single-owner Ruleset profile

The accepted automatic profile is:

- enforcement active on `~DEFAULT_BRANCH`;
- pull requests required;
- squash is the only merge method;
- branch deletion and non-fast-forward updates blocked;
- required review-thread resolution enabled;
- required approving review count `0`;
- last-push approval disabled;
- no Integration bypass actor;
- the existing RepositoryRole pull-request bypass is allowed, but the Worker App is not added to a bypass list.

The automatic profile deliberately omits GitHub's `Restrict updates` rule. That rule permits branch updates only by bypass actors; because the Worker App is intentionally not a bypass actor, enabling it would also block the Worker's compliant PR merge. Requiring a PR and blocking non-fast-forward updates still reject direct and history-rewriting pushes.

The current EaseWise Ruleset is this profile and must not be “repaired” back to simulated multi-person approval. A separate manual profile with one approval and last-push approval remains valid for repositories that choose it.

Auto-merge occurs only when both the local mode and repository profile are automatic. Any mismatch leaves the task in manual review or `needs-attention`; it never silently weakens repository policy.

## 6. Mainline Refresh Before Merge

The Worker must prove the delivery against the current default branch, not only the original context commit.

Immediately before delivery and again before merge, the Worker fetches the current default-branch head.

### Default branch unchanged

If the default head still equals the integrated base recorded for the task, the Worker proceeds with the verified delivery head.

### Default branch advanced

If the context commit remains an ancestor of the new default head, the Worker:

1. compares task-changed paths and default-branch changes since the last integrated base;
2. stops on overlapping or renamed paths;
3. creates a Worker-owned merge commit from the new default head into the task branch without invoking Codex;
4. recalculates the PR diff against the new base and re-runs path, size, secret, binary, and sensitive-path checks;
5. runs the repository-approved verification profile again;
6. updates the delivery head and evidence before pushing.

Integration conflicts, validation failures, or a non-ancestor default head enter `needs-attention`. The Worker never resolves an integration conflict by guessing.

The refresh loop is capped at two default-branch advances for one delivery. A third advance stops safely to avoid indefinite chasing on a busy branch. Worker-created integration commits are audited separately from the Codex-produced task commit.

## 7. Automatic Merge State Machine

The lifecycle adds `codex:merging` between verified delivery and completion.

```text
queued → claimed → running → verifying
                         ↓
               integration-refresh
                         ↓
               Draft PR + merging
                         ↓
                    completed
```

`awaiting-review` remains the terminal delivery state for manual mode. In automatic mode, a verified delivery enters `merging` and the Worker performs the following exact-head gate:

1. reconcile the Issue, task hash, delivery block, remote branch, Draft PR, and SQLite checkpoint;
2. verify the PR author is the currently attested Worker App bot;
3. verify low/medium task risk and unchanged acceptance, context, allowed paths, and verification profile;
4. verify the PR diff, protected paths, secret/binary scan, file/line limits, mergeability, required GitHub checks, and unresolved threads;
5. verify the single-owner Ruleset and local automatic mode;
6. verify the PR head equals the last integrated and tested commit;
7. mark the Draft PR ready;
8. repeat all remote-state gates;
9. call the squash merge API with the expected full head SHA;
10. read back GitHub merge state before marking the task completed and closing the Issue.

The human-assisted merge function remains separate and continues to require an independent actor. Worker auto-merge uses a distinct operation path so removing the human actor check cannot weaken manual repositories.

## 8. Durability, Retry, and Failure Handling

Before the first merge-related GitHub write, SQLite records an immutable auto-merge operation keyed by repository, Issue, PR, task hash, and exact head SHA.

After a crash or restart, the Worker reconciles GitHub before writing again:

- already merged with the expected head: finalize the original operation;
- still open at the expected head: continue the recorded operation;
- changed head, changed task, changed Ruleset, or ambiguous PR: stop;
- merge API response lost after success: confirm GitHub merge state rather than issuing a second merge.

Network failures, GitHub 429/5xx responses, and recoverable merge API failures receive at most two bounded retries. Authentication, permission, policy, conflict, scope, verification, or Ruleset failures do not retry automatically. No auto-merge retry invokes Codex or creates a replacement task commit.

The status comment records the integration base, verified head, merge attempt, merge commit, and failure classification. The Issue closes only after GitHub confirms the merge.

## 9. Existing Deliveries and EaseWise PR #13

Deployment uses an explicit migration boundary:

1. ship the new Worker with `merge_mode = "manual"` and verify no behavior change;
2. validate the current EaseWise single-owner Ruleset without mutating it;
3. set the trusted Mac mini configuration to `merge_mode = "automatic"`;
4. restart the Worker;
5. reconcile existing `awaiting-review` tasks as auto-merge candidates without rerunning Codex;
6. revalidate EaseWise PR #13 at head `de316cb82a6853b961c98c19eb288fa71958022b` against the current default branch;
7. auto-merge it only if every new gate passes.

The existing approval fingerprint is audit evidence but is not required for the automatic operation. If PR #13, its task block, default branch, Ruleset, or verification evidence changes during rollout, the Worker stops rather than grandfathering stale approval.

## 10. Test Environment and Rollback

Automatic merge ends at the repository default branch. Test-environment deployment remains the responsibility of the target repository's existing pipeline; the Worker does not acquire deployment permissions.

The owner evaluates behavior in the test environment. A bad result is reverted explicitly using the recorded merge commit. Version one does not auto-revert because an automated rollback could hide evidence, revert unrelated follow-up work, or amplify an incorrect diagnosis.

A later `codexctl task revert` workflow may provide a bounded, confirmation-based convenience command, but it is outside this implementation.

## 11. Verification Strategy

Automated coverage must prove:

- `merge_mode` defaults to manual and rejects unknown values;
- automatic mode requires the single-owner Ruleset and no Integration bypass;
- manual and automatic Ruleset profiles are distinguished deterministically;
- only the attested Worker bot can auto-merge its delivery;
- MacBook and queued-task path conflicts are rejected;
- unchanged-main delivery merges without an integration commit;
- advanced-main delivery integrates and re-verifies before merge;
- overlapping paths, conflicts, failed verification, or repeated main advances stop;
- exact-head changes before and after Ready block merge;
- missing/malformed App metadata remains fail-closed under the existing attested-bot rules;
- crash boundaries before Ready, after Ready, during merge, and after remote success are idempotent;
- transient merge failures retry without Codex and permanent failures do not;
- Issue completion occurs only after confirmed GitHub merge;
- manual mode retains the existing `codexctl review/merge` behavior;
- the dispatch skill may delegate within an authorized parent objective but refuses scope expansion and high-risk work.

The rollout must also complete a live EaseWise drill: installed commit and PID verification, GitHub App API access, PR #13 exact-head reconciliation, automatic squash merge, Issue completion, and confirmation that no second Codex run or duplicate PR occurred.

## 12. Non-Goals

This version does not add multiple Mac mini Workers, central scheduling, automatic product decisions, automatic production deployment, automatic database migration, automatic rollback, high-risk tasks, or Codex Goal mode. It also does not remove GitHub Issues or PRs: they remain the cross-device queue, audit log, and recovery source of truth.
