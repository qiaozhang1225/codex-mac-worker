---
name: dispatch-codex-task
description: Use when onboarding a repository for the Mac mini Worker, deciding whether MacBook work should be delegated, dispatching or controlling a bounded Worker task, or reviewing a Worker delivery.
---

# Dispatch Codex Task

## Role and boundary

Act as the MacBook **principal development agent**. Develop directly when that is faster or safer; delegate only an independently verifiable **strict subset of the authorized parent objective**. Mac mini cannot further delegate, widen scope, choose new verification commands, deploy, access production data, or use Codex Goal mode.

## Repository readiness

Run `codexctl repo status OWNER/REPO`. Require `phase: ready`, schema v2, the intended numeric `worker_github_app_id`, and a recognized Ruleset profile. If unconfigured, use `codexctl repo onboard`, show its exact PR/head/paths/blockers, and run the following only after explicit approval of that snapshot:

```bash
codexctl repo finalize OWNER/REPO#PR --expected-head FULL_HEAD_SHA
```

Wait for `ready` before dispatch.

## Decide whether to delegate

Keep tightly coupled, exploratory, high-risk, hard-to-verify, or product-judgment work local. Delegate only when risk is low/medium, the objective and acceptance are observable, `allowed_paths` are minimal, and the repository owns the verification profile.

Before dispatch, check GitHub Issue **active path ownership**, `git status`, and MacBook planned paths. Delegated paths must not overlap protected, locally changed, planned, or active Worker paths. If ownership or scope is uncertain, keep it local, narrow it, or sequence it.

## Dispatch

Read `.codex-worker/project.toml` and relevant `AGENTS.md`. Verify context is committed and pushed with `git rev-parse`, upstream ancestry, and `git push`. Freeze `context_commit`, `base_branch`, one objective, acceptance, tracked `context_files`, minimal `allowed_paths`, `verification_profile`, and risk. The agent must refuse production deployment/data, credentials, migrations, irreversible work, high risk, unverifiable acceptance, and oversized scope.

Choose one boundary:

1. For a standalone owner request, show the complete final specification and obtain confirmation before creation.
2. For delegation inside the currently authorized parent development objective, after subset, context, policy, and conflict checks pass, do not ask again:

   ```bash
   codexctl task create --yes --repo OWNER/REPO --spec task.yaml
   ```

Record the Issue URL and active paths before continuing non-conflicting local work.

## Control

Inspect with `codexctl task status ISSUE_URL`. Use only a legal command for the current state: `codexctl task revise`, `pause`, `resume`, `retry`, or `cancel`. A revision stays inside the original contract and starts a new session. `retry` is infrastructure-only and never reruns Codex after a delivery checkpoint.

## Merge policy

- With `merge_mode = "automatic"` and the automatic Ruleset profile, monitor `codex:merging`. Worker rechecks current main, scope, checks, threads, identity, and exact head before squash merge. Do not issue a manual merge or simulate review.
- In manual mode, run `codexctl task review ISSUE_URL` and show the PR, head SHA, fingerprint, gates, evidence, risks, and dependencies. Explicit confirmation of that exact snapshot is required before `codexctl task merge ISSUE_URL --expected-head FULL_HEAD_SHA --expected-fingerprint APPROVAL_FINGERPRINT`.

Any drift invalidates a manual snapshot. Automatic merge ends at the default branch; test observation, production deployment, and rollback remain separate decisions. Never treat automatic merge as permission to bypass failed checks, unresolved threads, scope limits, or exact-head validation.
