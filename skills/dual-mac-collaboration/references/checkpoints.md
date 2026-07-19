# Checkpoints and Evidence

Write each event as `<!-- duomac-event:v1 -->` followed by one fenced YAML block. Always fetch the Issue immediately before writing and require the event revision to equal the body revision.

## Task start

```yaml
type: task-start
revision: 1
skill_commit: 40-character-skill-commit-SHA
base_commit: 40-character-task-base-SHA
plan_summary:
  - Current execution stage
```

Publish this only after validating the contract and repository configuration. It moves the Issue to `duomac:active`.

## Milestone checkpoint

```yaml
type: checkpoint
revision: 1
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

Publish after each planned milestone. If blockers is empty, scope remains valid, and the Issue revision is unchanged, continue directly to the next milestone. Do not wait for a checkpoint approval.

## Blocked event

```yaml
type: blocked
revision: 1
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
revision: 1
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

