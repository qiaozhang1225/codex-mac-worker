# Git Delivery

## Create isolated work

Fetch the default branch and confirm the contract context commit is available from the remote. Record the fetched default-branch SHA as `start_base`. Create a `codex/<issue>-<slug>` branch and an isolated worktree from that SHA. Never work from a dirty primary checkout.

Before implementation, verify the current branch, worktree path, context ancestry, current Issue revision, and allowed paths. Keep commits small enough to trace to planned milestones.

## Preflight and verification

Before delivery require all of the following:

- The worktree is clean and HEAD is an attached `codex/*` branch.
- Every changed path is inside an allowed path and outside protected paths.
- File and diff limits from `.duomac/project.toml` are satisfied.
- There are no submodule changes, binary changes, or tracked `.env*` changes.
- The latest Issue revision still matches all evidence.
- The selected verification profile passes using commands from project configuration.

Use the preflight helper before verification and again after any rebase or verification command that could modify files.

## Direct-main drift handling

Push the verified task HEAD to the default branch without changing local branch identity.

| Remote state after fetch | Action |
|---|---|
| Still equals `start_base` | Push normally to the default branch |
| Advanced; changed paths do not overlap task paths | Rebase once onto the fetched tip, repeat preflight, rerun the full selected verification profile, then push normally |
| Advanced; any path overlaps | Stop and record blocked evidence |
| Diverged, rebase conflicts, or push is rejected after the one refresh | Stop and record blocked evidence |

Never force push, never retry an unbounded number of times, and never resolve a semantic conflict by guessing product intent. A successful direct-main delivery records the resulting commit and closes the Issue as completed.

## Task-branch delivery

Push `HEAD` only to the matching `codex/*` remote branch. Do not update the default branch or silently change modes. Record the branch commit and delivery evidence, label the Issue delivered, and leave integration to MacBook.

## Cleanup

Remove a task worktree only after its commit exists on the intended remote ref and the Issue contains delivery evidence. Keep blocked worktrees until the blocker is resolved or the user explicitly cancels and chooses cleanup.

