# Assisted Merge GitHub App Gate Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the two false-positive assisted-merge blockers observed on EaseWise PR #13 while preserving fail-closed Worker identity and operational-risk checks.

**Architecture:** Keep the repository readiness attestation as the authoritative Worker identity. Require an exact GitHub Bot login match, corroborate the App ID whenever the PR payload provides App metadata, and treat absent PR App metadata as unavailable rather than contradictory evidence. Replace the bare production-word risk match with explicit operational terms and production-resource phrases.

**Tech Stack:** Python 3.12, `re`, pytest, existing `assisted_merge` review fixtures.

## Global Constraints

- Do not change merge execution, approval fingerprints, Ruleset validation, task policy, review-thread checks, Worker execution, or deployment behavior.
- Missing repository attestation, non-bot PR authors, mismatched bot logins, and mismatched present App IDs must remain blocked.
- Bare `production`, `prod`, and `生产` must not be sufficient to block a delivery risk note.
- Credentials, secrets, passwords, deployment, migration, irreversible work, production data, production databases, and production environments must remain blocked in English and Chinese.
- EaseWise PR #13 must remain unmerged until the deployed fix produces a fresh live review snapshot and the user separately approves its exact head and fingerprint.

---

### Task 1: Accept missing PR App metadata only for the attested bot

**Files:**
- Modify: `tests/test_assisted_merge.py`
- Modify: `src/codex_mac_worker/assisted_merge.py`

**Interfaces:**
- Consumes: `_authoritative_worker_identity(github, repo) -> tuple[str, int] | None`.
- Produces: `review_task(...)` identity blockers based on `pull.user.login`, `pull.user.type`, and optional `pull.performed_via_github_app.id`.

- [ ] **Step 1: Prepare the isolated test environment and establish a green baseline**

Run:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q tests/test_assisted_merge.py
```

Expected: dependency installation succeeds and the unchanged assisted-merge test file passes.

- [ ] **Step 2: Write failing identity regression tests**

Add `"type": "Bot"` to the happy-path pull user fixture, add mutations for missing App metadata, a non-bot author, and a mismatched bot login, then add these tests:

```python
def test_review_allows_attested_bot_when_pull_app_metadata_is_absent() -> None:
    from codex_mac_worker.assisted_merge import review_task

    github = ReviewGitHub.happy_path()
    github.pull["performed_via_github_app"] = None

    snapshot = review_task(github, IssueReference("owner/repo", 12))

    assert snapshot.gates.allowed is True


@pytest.mark.parametrize(
    ("user", "blocker"),
    [
        ({"login": "worker-app[bot]", "type": "User"}, "Bot"),
        ({"login": "other-worker[bot]", "type": "Bot"}, "identity"),
    ],
)
def test_review_blocks_unattested_pull_author(user: dict[str, str], blocker: str) -> None:
    from codex_mac_worker.assisted_merge import review_task

    github = ReviewGitHub.happy_path()
    github.pull["user"] = user
    github.pull["performed_via_github_app"] = None

    snapshot = review_task(github, IssueReference("owner/repo", 12))

    assert snapshot.gates.allowed is False
    assert any(blocker.lower() in item.lower() for item in snapshot.gates.blockers)
```

Retain the existing `different_app` case so a present App ID of `999` remains blocked against attested App ID `777`.
Also parameterize malformed non-null metadata (`str`, `list`, `int`, empty dictionaries, missing IDs, and string IDs) and require the GitHub App blocker for every case.

- [ ] **Step 3: Run the focused tests and confirm the missing-metadata case fails**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_assisted_merge.py -k 'attested_bot or unattested_pull_author or different_app'
```

Expected: the missing-metadata test fails with `PR was not created by the attested Worker GitHub App`; existing happy-path and mismatched-App coverage still behave as before.

- [ ] **Step 4: Implement the minimal identity gate**

Replace the PR author extraction and identity comparison in `review_task` with:

```python
pull_user = pull.get("user", {})
author_login = str(pull_user.get("login", ""))
author_type = str(pull_user.get("type", ""))
pull_app_metadata = pull.get("performed_via_github_app")
pull_app_id = (
    pull_app_metadata.get("id")
    if isinstance(pull_app_metadata, dict)
    else None
)
if worker_identity is None:
    blockers.append(
        "Worker identity has no current attestation from the trusted Worker GitHub App"
    )
else:
    worker_login, worker_app_id = worker_identity
    if author_type != "Bot":
        blockers.append("PR author is not a GitHub Bot")
    if author_login != worker_login:
        blockers.append("PR author does not match the attested Worker identity")
    if pull_app_metadata is not None and (
        not isinstance(pull_app_metadata, dict)
        or pull_app_id != worker_app_id
    ):
        blockers.append("PR was not created by the attested Worker GitHub App")
```

- [ ] **Step 5: Run the identity tests and confirm they pass**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_assisted_merge.py -k 'attested_bot or unattested_pull_author or different_app or snapshot_binds'
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the identity fix**

```bash
git add src/codex_mac_worker/assisted_merge.py tests/test_assisted_merge.py
git commit -m "Fix Worker PR identity attestation gate"
```

### Task 2: Distinguish build warnings from production operations

**Files:**
- Modify: `tests/test_assisted_merge.py`
- Modify: `src/codex_mac_worker/assisted_merge.py`

**Interfaces:**
- Consumes: `DeliveryMetadata.risks: tuple[str, ...]` from the signed delivery block.
- Produces: `_UNSAFE_RISK_RE`, which matches explicit high-risk or operational language without matching bare build-context production words.

- [ ] **Step 1: Write failing risk-classification regression tests**

Add a focused helper and parameterized tests:

```python
def github_with_delivery_risk(risk: str) -> ReviewGitHub:
    github = ReviewGitHub.happy_path()
    github.pull["body"] = github.pull["body"].replace(
        "risks: []", f"risks:\n- {risk}"
    )
    return github


@pytest.mark.parametrize(
    "risk",
    [
        "Production build has an existing bundle-size warning",
        "生产构建存在既有的大分块体积警告",
    ],
)
def test_review_allows_benign_production_build_risk(risk: str) -> None:
    from codex_mac_worker.assisted_merge import review_task

    snapshot = review_task(
        github_with_delivery_risk(risk), IssueReference("owner/repo", 12)
    )

    assert snapshot.gates.allowed is True


@pytest.mark.parametrize(
    "risk",
    [
        "Production data may be modified",
        "Production database migration is required",
        "Production environment deployment is required",
        "需要修改生产数据",
        "需要迁移生产数据库",
        "需要部署到生产环境",
        "密码可能泄露",
        "生产 数据可能被修改",
        "生产-数据库可能被修改",
    ],
)
def test_review_blocks_explicit_production_operation_risk(risk: str) -> None:
    from codex_mac_worker.assisted_merge import review_task

    snapshot = review_task(
        github_with_delivery_risk(risk), IssueReference("owner/repo", 12)
    )

    assert snapshot.gates.allowed is False
    assert any("risks" in item for item in snapshot.gates.blockers)
```

- [ ] **Step 2: Run the focused tests and confirm benign build notes fail**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_assisted_merge.py -k 'production_build_risk or production_operation_risk or credential_risk'
```

Expected: both benign production-build cases fail because the current expression matches bare `production` and `生产`; all explicit operational cases are blocked.

- [ ] **Step 3: Narrow the unsafe-risk expression**

Replace `_UNSAFE_RISK_RE` with:

```python
_UNSAFE_RISK_RE = re.compile(
    r"\b(high[-\s]?risk|credentials?|secrets?|passwords?|"
    r"deploy(?:ment|ed|ing)?|migrations?|irreversible|"
    r"prod(?:uction)?[\s_-]+(?:data|databases?|environments?))\b|"
    r"高风险|凭据|密钥|密码|部署|迁移|不可逆|生产[\s_-]*(?:数据|数据库|环境)",
    re.IGNORECASE,
)
```

- [ ] **Step 4: Run the focused risk tests and confirm they pass**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_assisted_merge.py -k 'production_build_risk or production_operation_risk or credential_risk'
```

Expected: all selected tests pass; benign build warnings are allowed and explicit operational risks remain blocked.

- [ ] **Step 5: Commit the risk matcher fix**

```bash
git add src/codex_mac_worker/assisted_merge.py tests/test_assisted_merge.py
git commit -m "Narrow assisted merge operational risk matching"
```

### Task 3: Verify and publish the Worker fix

**Files:**
- Verify: `src/codex_mac_worker/assisted_merge.py`
- Verify: `tests/test_assisted_merge.py`
- Verify: `docs/superpowers/specs/2026-07-16-assisted-merge-github-app-gate-design.md`
- Verify: `docs/superpowers/plans/2026-07-16-assisted-merge-github-app-gate-fix.md`

**Interfaces:**
- Consumes: the two committed gate fixes.
- Produces: a reviewed Draft PR for `qiaozhang1225/codex-mac-worker`; it does not merge or deploy it.

- [ ] **Step 1: Run the complete test suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 2: Compile the source and build the package**

The repository has no separate lint or type-check configuration. Run:

```bash
.venv/bin/python -m compileall -q src tests
mkdir -p /tmp/codex-mac-worker-wheel-gate-fix
.venv/bin/pip wheel --no-deps . --wheel-dir /tmp/codex-mac-worker-wheel-gate-fix
```

Expected: compilation exits `0` and pip creates one `codex_mac_worker-0.1.0-py3-none-any.whl` file.

- [ ] **Step 3: Review the exact diff and commit history**

Run:

```bash
git diff --check origin/main...HEAD
git status --short
git log --oneline origin/main..HEAD
git diff --stat origin/main...HEAD
```

Expected: no whitespace errors, a clean worktree, and changes limited to the design, plan, assisted-merge implementation, and focused tests.

- [ ] **Step 4: Push and create a Draft PR**

```bash
git push -u origin codex/assisted-merge-gate-fix
gh pr create --draft --base main --head codex/assisted-merge-gate-fix \
  --title "Fix assisted merge GitHub App gates" \
  --body-file /tmp/codex-assisted-merge-gate-pr.md
```

Expected: GitHub returns a new Draft PR URL. Stop before merge or deployment and report its exact head SHA, checks, and review findings for explicit approval.
