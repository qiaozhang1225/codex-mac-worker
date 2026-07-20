# Checkpoints and Evidence

Write each event as `<!-- duomac-event:v1 -->` followed by one fenced YAML block. Always fetch the Issue immediately before writing and require the event revision to equal the body revision.

## Task start

```yaml
type: task-start
revision: 2
task_hash: 64-character-lowercase-sha256
repository: owner/repository
base_branch: main
context_commit: 40-character-context-commit-SHA
skill_commit: 40-character-skill-commit-SHA
base_commit: 40-character-task-base-SHA
plan_summary:
  - Execute milestone 1 objective
  - Execute milestone 2 objective
execution_mode: scheduled
slot: 2
claim_id: 40-character-lowercase-claim-id
```

Publish this only after validating the schema v2 contract and repository configuration. It moves the Issue to `duomac:active`. Scheduled starts require `execution_mode: scheduled`, a slot from 1 through 3, and the picker-issued `claim_id`; interactive starts use `execution_mode: interactive` and omit `slot` and `claim_id`.

## Milestone checkpoint

```yaml
type: checkpoint
revision: 2
milestone: 2
completed:
  - Completed work
commits:
  - 40-character-commit-SHA
verification:
  - command: passed
scope_status: within-scope
next:
  - Next approved step
blockers: []
```

Publish exactly one checkpoint after each declared milestone, in continuous order beginning at milestone 1. Never begin the next milestone before the current checkpoint exists. If blockers is empty, scope remains valid, and the Issue revision is unchanged, continue directly to the next milestone. A checkpoint is evidence, not an approval gate; do not wait for MacBook approval.

The final milestone checkpoint must precede delivery evidence. Completion requires the full current-revision sequence: one authoritative task-start, every declared milestone checkpoint in order, then delivery. Discover the exact completion interface with `issue_complete.py --help` before use.

## Blocked event

```yaml
type: blocked
revision: 2
reason: Why the current contract cannot safely continue
completed:
  - Safe work already retained
next:
  - Decision or body revision required
```

Use blocked for unresolved product choices, a revision that invalidates completed work, scope or protected-path violations, conflicting remote changes, missing access, verification that cannot be repaired inside scope, or operational risk. Preserve useful local evidence and do not improvise a wider contract.

## Delivery evidence

```yaml
type: delivery
revision: 2
delivery_mode: direct-main
commit: 40-character-delivery-commit-SHA
changed_paths:
  - product/frontend/src/history/card.tsx
acceptance_results:
  - criterion: The approved criterion
    status: met
    evidence: Test or inspection evidence
verification:
  - command: passed
remaining_risks: []
```

Record actual paths and exact commands. Use `status: not-met` rather than hiding an unmet criterion; do not finalize delivery in that case. A task branch becomes `duomac:delivered` and stays open. A direct-main delivery becomes `duomac:completed` and closes only after the commit is on the default branch.
