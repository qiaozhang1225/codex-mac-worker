---
name: dual-mac-collaboration
description: Use when deciding whether MacBook work should be delegated, publishing or revising a GitHub Issue for Mac mini, executing a dispatched task interactively or with Codex App Scheduled, recording checkpoints, or delivering code through direct-main or task-branch.
---

# Dual-Mac Collaboration

Coordinate visible interactive or Codex App Scheduled work through one versioned GitHub Issue contract. Never use an external daemon, `codex exec`, LaunchDaemon, or Goal mode to execute tasks.

## Detect the role

Identify the current device role before acting. If it is unclear, ask whether this conversation is on the MacBook or Mac mini.

- On **MacBook**, read [roles and delegation](references/roles-and-delegation.md) before deciding whether to keep or delegate work. Read [the Issue protocol](references/issue-protocol.md) before drafting, publishing, or revising a task.
- On **Mac mini interactive**, read [roles and delegation](references/roles-and-delegation.md) and [the Issue protocol](references/issue-protocol.md) before accepting work. Read [checkpoints](references/checkpoints.md) before starting or reporting work. Read [Git delivery](references/git-delivery.md) before creating a worktree or delivering code.
- On **Mac mini Codex App Scheduled**, read and follow [Scheduled execution](references/scheduled-execution.md) before running any picker or mutating GitHub. Do not combine that route with interactive pickup.

Before planning or executing any milestone on Mac mini, read the applicable repository `AGENTS.md` instructions and every Issue-declared context file from the frozen context commit. If any required instruction or context file is missing or unreadable at that commit, publish blocked and execute nothing; never substitute current-checkout content or silently skip it.

## Dispatch from MacBook

1. Decide with the user whether MacBook should implement the work or delegate it. Duration alone is never a delegation reason.
2. Refuse to publish while any product decision, acceptance criterion, allowed path, context commit, or continuous execution step is unresolved. Identify the missing decisions and finish the discussion first.
3. Validate that context files are committed and pushed and that active work does not own overlapping paths.
4. Draft one complete Issue body. Use `direct-main` by default; select `task-branch` for concurrent or separately integrated work.
5. Show the final contract and obtain **explicit user confirmation** for Issue creation. Design approval, plan approval, or a prior general instruction does not count.
6. Preview and validate before creation. Run each command with `--help` first when its local interface is not already known:

```text
python scripts/issue_validate.py --help
python scripts/issue_create.py --help
```

7. Run `issue_create.py` without `--yes` to preview. Add `--yes` only after the confirmation in step 5.

## Execute on Mac mini

1. Fetch work only after the user opens or directs the visible Codex App. Select a `duomac:ready` Issue; do not poll in the background.
2. Treat the **Issue body is the only current task contract**. A comment may report evidence or request a revision, but it cannot expand scope. Refuse comment-only additions and request a complete body revision.
3. Require schema v2, validate the latest body and `.duomac/project.toml`, and read the frozen repository instructions and context above. Stop blocked before planning if the revision, context commit, product decisions, paths, risk, milestones, verification profile, `AGENTS.md`, or any declared context file is invalid, missing, or unreadable.
4. Publish `task-start`, create an isolated `codex/*` worktree, and implement only the approved plan.
5. Execute milestones in declared order. At every milestone, publish its structured checkpoint before starting the next milestone and **continue without MacBook approval** while the current revision remains valid and no hard stop is present.
6. Re-read the Issue body before every milestone and final delivery. If revision changed, finish the current safe check, validate the complete replacement contract, and never deliver evidence for the old revision.
7. Preflight, run the configured verification profile, and deliver with the selected mode. Discover exact helper interfaces when needed:

```text
python scripts/issue_checkpoint.py --help
python scripts/git_preflight.py --help
python scripts/git_deliver.py --help
python scripts/issue_complete.py --help
python scripts/config_validate.py --help
python scripts/scheduled_pick.py --help
```

The final declared milestone checkpoint must exist before delivery evidence. Use `issue_complete.py --help` to discover the completion gate; checkpoints are evidence, not approval gates.

## Stop conditions

Stop and mark the Issue blocked when execution requires a product decision, exceeds allowed paths or limits, touches protected or operational systems, encounters overlapping remote changes, cannot validate the current revision, or would change the delivery mode. Do not deploy, use production data, delegate the task again, expand scope, or create a new task Issue from Mac mini.

For `direct-main`, permit at most one non-conflicting rebase and rerun the full selected verification profile afterward. For `task-branch`, push only the task branch and leave the Issue delivered for later integration. **Never force push.**
