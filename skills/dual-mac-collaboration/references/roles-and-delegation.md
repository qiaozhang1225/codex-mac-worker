# Roles and Delegation

## MacBook authority

The MacBook is both a development device and the only dispatcher. The user and MacBook Codex jointly decide whether to keep work local or send it to Mac mini. MacBook may explore, make product judgments, write PRDs and specs, validate technology, implement directly, split work, or draft the task contract.

Only the user can authorize formal Issue creation. Ask for a new explicit confirmation after showing the complete final contract. Do not treat approval of a PRD, design, technical plan, or this collaboration model as permission to publish a particular task.

MacBook owns contract revisions, cancellation, delivery-mode changes, and decisions about integrating a task branch.

## Delegation eligibility

Delegate only when every answer is yes:

- Product behavior, wording, UX, and tradeoffs are decided.
- The objective has one bounded result and objective acceptance criteria.
- Relevant PRD, Project Card, Spec, design, and technical evidence are committed and pushed.
- Allowed paths are minimal, do not overlap other active work, and exclude protected or operational paths.
- The execution plan is complete and continuous from setup through verification and delivery.
- Risk is low or medium and no production data, deployment, irreversible operation, or new privilege is needed.
- Mac mini can resolve implementation details without making a product decision.

A five-minute task may stay on MacBook. A multi-day task may go to Mac mini when the contract is complete. Estimated time is not a gate.

## Mac mini authority

Mac mini may choose implementation details within the current body, edit only allowed paths, run approved verification, create normal commits, record checkpoints, perform one permitted non-conflicting rebase, and deliver using the selected mode.

Mac mini must not widen scope, change acceptance, choose product behavior, change delivery mode, deploy, access production data, make irreversible changes, publish another delegated task, or infer authority from a comment. When one of these is needed, record a blocked event and return the decision to MacBook.

Long-task checkpoints are evidence, not approval gates. Continue through the remaining approved milestones without waiting unless the contract changes or a hard stop appears.

## Path ownership

Before dispatch, compare allowed paths against MacBook's uncommitted work and active dual-Mac Issues. Do not assign overlapping paths. During execution, treat any overlap with a concurrent remote change as a hard stop; proximity alone is not permission to reconcile product behavior.

