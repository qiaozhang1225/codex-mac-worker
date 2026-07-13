---
name: dispatch-codex-task
description: Use when onboarding a repository for the Mac mini Worker, dispatching or controlling a bounded Worker task, reviewing its PR, or handling a request to merge that PR.
---

# Dispatch Codex Task

## Overview

Manage one repository or bounded task at a time. Each mutation has an immutable snapshot and explicit confirmation boundary.

## Repository Readiness

1. Run `codexctl repo status OWNER/REPO`; refuse dispatch unless `phase` is `ready`.
2. Otherwise use `codexctl repo onboard`, then show PR, head SHA, paths, and blockers.
3. Stop. Only explicit approval naming that PR authorizes:

   ```bash
   codexctl repo finalize OWNER/REPO#PR --expected-head FULL_HEAD_SHA
   ```

4. After finalize, wait for `awaiting-worker` to become `ready`.

## Create a Task

1. Read `.codex-worker/project.toml`, relevant `AGENTS.md`, then verify immutable remote context:

   ```bash
   git status --short
   git rev-parse HEAD
   git rev-parse --verify '@{upstream}'
   git merge-base --is-ancestor HEAD '@{upstream}'
   ```

   Require a clean tree and HEAD on upstream history. Otherwise require commit and `git push`.
2. Produce exactly one deliverable with:

   - one observable `objective` and verifiable `acceptance`;
   - tracked `context_files` and minimal `allowed_paths`;
   - a configured `verification_profile` and low/medium `risk`.

3. Check protected paths and limits. Refuse unrelated outcomes, high risk, deployment, production data, irreversible operations, unverifiable work, or oversized scope.
4. Write `task.yaml`; show the complete specification and command:

   ```bash
   codexctl task create --repo OWNER/REPO --spec task.yaml
   ```

5. Run only after explicit confirmation of that final specification.

## Operate an Existing Task

Inspect with `codexctl task status ISSUE_URL`. Show one legal control command for the current state and obtain explicit confirmation before posting it:

```bash
codexctl task revise ISSUE_URL --requirements revision.yaml
codexctl task pause ISSUE_URL
codexctl task resume ISSUE_URL
codexctl task retry ISSUE_URL
codexctl task cancel ISSUE_URL
```

A revision stays bounded on the original branch and Draft PR.

## Review and Merge One Delivery

1. Run read-only `codexctl task review ISSUE_URL`; show PR, head SHA, fingerprint, gates, Checks, evidence, risks, and dependencies.
2. Stop. Only explicit approval naming that PR or current snapshot authorizes:

   ```bash
   codexctl task merge ISSUE_URL --expected-head FULL_HEAD_SHA --expected-fingerprint APPROVAL_FINGERPRINT
   ```

3. Any SHA, check, task, thread, or Ruleset change requires fresh review and approval.

Design approval, repository-wide approval, “看起来可以”, “设计通过”, old-thread approval, and approval for a future PR are not merge authorization. There is no automatic merge or standing approval, including indirect GitHub or workflow auto-merge.

## Safety Contract

- Do not use Codex Goal/“目标” mode or persistent objectives.
- Do not expand `allowed_paths`, acceptance, permissions, or risk; Issues select only repository-approved verification profiles.
- Do not request credentials, production access, deployment, Ruleset bypass, automatic merge, or protected-branch pushes.
- Never authorize future PRs or treat a repository-level preference as approval.
- Never invoke `repo finalize` or `task merge` without the immediately preceding immutable snapshot and explicit one-PR approval.
