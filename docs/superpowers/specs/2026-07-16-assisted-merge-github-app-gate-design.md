# Assisted Merge GitHub App Gate Fix

## Problem

EaseWise PR #13 was created by the trusted Worker GitHub App bot, but assisted merge blocked it for two unrelated reasons:

1. GitHub's pull request REST payload returned `performed_via_github_app: null`. The current gate treats missing PR App metadata as proof that the PR was not created by the attested App.
2. The delivery risk note mentions a benign “production build” warning. The current regular expression treats every occurrence of `production` or `生产` as high-risk operational work.

The fix must remove these false positives without weakening protection against impersonated PR authors or real production operations.

## Considered Approaches

### 1. Require PR App metadata unconditionally

This preserves the existing code but cannot review legitimate GitHub App PRs when GitHub omits the field. Rejected because it makes assisted merge unusable for the observed API response.

### 2. Trust only the PR author's bot login

This handles the missing field, but discards useful App ID evidence when GitHub does provide it. Rejected because it weakens the gate unnecessarily.

### 3. Use attested bot identity with optional PR App corroboration

This is the selected approach. The repository readiness attestation remains the source of truth for the trusted bot login and App ID. A PR must be authored by a GitHub `Bot` whose login exactly matches that attestation. When the PR payload also contains App metadata, its App ID must match; when GitHub omits it, the exact attested bot identity is sufficient.

## Design

### Worker identity gate

`review_task` will read the PR author's login and account type in addition to the optional `performed_via_github_app` payload.

- Block if there is no current repository attestation from the configured Worker App.
- Block if the PR author is not a `Bot`.
- Block if the PR author's login differs from the attested Worker bot login.
- If PR App metadata is present, block when its ID differs from the attested App ID.
- If PR App metadata is absent, do not create an App-ID blocker after the exact attested bot checks pass.

The repository probe and attestation validation is unchanged: it still binds the identity to the current project configuration and configured `worker_github_app_id`.

### Risk text gate

The risk matcher will continue to block unambiguous high-risk and operational terms such as credentials, secrets, passwords, deployment, migration, irreversible work, production data, production databases, production environments, and their Chinese equivalents.

The bare words `production`, `prod`, and `生产` will no longer be sufficient by themselves. This allows benign evidence such as “production build has an existing bundle-size warning” while continuing to block phrases such as “production deployment” and “生产数据变更”.

### Scope

Only `assisted_merge.py` and its focused tests will change. The merge API, approval fingerprint, ruleset checks, task policy, review-thread checks, Worker execution, and deployment behavior remain unchanged.

## Error Handling and Security Properties

Ambiguous identity remains fail-closed: missing attestation, non-bot authors, mismatched logins, and mismatched non-null App IDs all block review. A missing field in GitHub's PR representation is treated as unavailable evidence, not contradictory evidence.

Risk classification also remains fail-closed for explicit operational phrases. This change is a narrow lexical correction and does not permit high-risk Issue specifications, protected paths, deployment commands, or production operations elsewhere in the policy system.

## Testing

Focused regression tests will cover:

- a matching attested bot with null PR App metadata is accepted;
- present matching App metadata is accepted;
- present mismatched App metadata is blocked;
- a matching login with non-bot account type is blocked;
- a mismatched bot login is blocked;
- benign English and Chinese production-build risk notes are accepted;
- explicit English and Chinese production data, environment, and deployment risks are blocked.

The complete test suite will run before publishing a Draft PR. After deployment, EaseWise Issue #12 will be reviewed again from live GitHub state; PR #13 will remain unmerged until its exact new review fingerprint receives separate approval.
