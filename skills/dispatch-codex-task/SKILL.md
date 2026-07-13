---
name: dispatch-codex-task
description: Use when turning a product or engineering request into a bounded GitHub Issue for the Codex Mac mini Worker, or when checking, revising, pausing, resuming, retrying, cancelling, or reviewing such a task.
---

# Dispatch Codex Task

## Overview

Convert one clear, low- or medium-risk outcome into a frozen Worker task. Keep the human in control of task creation and merge.

## Create a Task

1. Locate the repository root and read `.codex-worker/project.toml` plus relevant `AGENTS.md` files.
2. Confirm the context is immutable and available remotely:

   ```bash
   git status --short
   git rev-parse HEAD
   git rev-parse --verify '@{upstream}'
   git merge-base --is-ancestor HEAD '@{upstream}'
   ```

   Require a clean working tree. If HEAD is not on the upstream history, stop and ask the user to commit and `git push`; do not create the task from local-only context.
3. Shape the request as exactly one deliverable:

   - `objective`: one observable result.
   - `acceptance`: concrete, independently verifiable checks.
   - `context_files`: tracked files that explain the task.
   - `allowed_paths`: the smallest directories or files required.
   - `verification_profile`: a profile defined by project configuration.
   - `risk`: only a configured low or medium value.

4. Compare the proposed scope with protected paths and resource limits. Explicitly refuse dispatch when the request combines unrelated outcomes, needs high-risk access, includes deployment or production data, permits irreversible actions, or cannot be verified. Ask the user to split an oversized request.
5. Write a temporary `task.yaml`, then show the complete specification and this exact action:

   ```bash
   codexctl task create --repo OWNER/REPO --spec task.yaml
   ```

6. Wait for explicit user confirmation. Only after confirmation, run the command. Never treat an earlier general request as confirmation of the final task specification.

## Operate an Existing Task

Use `codexctl task status ISSUE_URL` for inspection. For control actions, show the exact command and obtain confirmation before creating the structured GitHub comment:

```bash
codexctl task revise ISSUE_URL --requirements revision.yaml
codexctl task pause ISSUE_URL
codexctl task resume ISSUE_URL
codexctl task retry ISSUE_URL
codexctl task cancel ISSUE_URL
```

A revision must contain explicit, bounded requirements and stays on the original branch and Draft PR.

## Safety Contract

- Do not invoke Codex Goal/“目标” mode or create a persistent autonomous objective.
- Do not expand `allowed_paths`, acceptance criteria, permissions, or risk to make a task easier to dispatch.
- Do not place test commands in the Issue; select only repository-approved verification profiles.
- Do not request credentials, production access, deployment, GitHub ruleset bypass, automatic merge, or direct pushes to protected branches.
- Do not merge a PR. The user retains final review and merge authority.
