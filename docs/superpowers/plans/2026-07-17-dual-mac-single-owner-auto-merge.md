# Dual Mac Single-Owner Auto-Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the MacBook Codex agent develop directly or autonomously delegate bounded work to the Mac mini, and let the Mac mini automatically squash-merge its own verified low/medium-risk deliveries under an explicit single-owner repository policy.

**Architecture:** Add a trusted local `merge_mode` and deterministic manual/automatic Ruleset profiles, then keep human-assisted merge and Worker auto-merge as separate code paths. Add pre-dispatch path ownership checks and a bounded current-main integration refresh before delivery. Persist exact-head auto-merge operations and route their GitHub writes through the existing durable outbox so restarts reconcile instead of repeating Codex or creating duplicate PRs.

**Tech Stack:** Python 3.12, SQLite WAL, GitHub REST/GraphQL APIs, Git worktrees, pytest, macOS launchd, Markdown Codex skill instructions.

## Global Constraints

- The MacBook remains a full development agent and may implement work directly or delegate a strict subset of an already authorized parent objective.
- The Mac mini never decomposes tasks, expands scope, selects new verification commands, deploys, accesses production data, or invokes Codex Goal mode.
- Only low/medium-risk tasks with frozen objective, acceptance, context, paths, and repository-approved verification may execute.
- Auto-merge requires both trusted local `merge_mode = "automatic"` and the recognized single-owner Ruleset profile.
- Automatic merge ends at `main`; test-environment deployment and rollback remain outside Worker permissions.
- Manual mode remains the default and preserves the existing `codexctl task review/merge` path.
- No Integration actor may appear in a Ruleset bypass list.
- Mainline integration refresh is bounded to two advances and never asks Codex to resolve conflicts.
- Every remote write is exact-head, durable, idempotent, and reconciled after ambiguous failures.
- EaseWise PR #13 is migrated in place without rerunning Codex or creating another PR.

---

### Task 1: Trusted Merge Mode and Ruleset Profiles

**Files:**
- Create: `src/codex_mac_worker/merge_policy.py`
- Modify: `src/codex_mac_worker/config.py`
- Modify: `src/codex_mac_worker/repository_onboarding.py`
- Modify: `src/codex_mac_worker/assisted_merge.py`
- Modify: `templates/worker.toml.example`
- Test: `tests/test_worker_config.py`
- Test: `tests/test_repository_onboarding.py`
- Test: `tests/test_assisted_merge.py`

**Interfaces:**
- Produces: `MANUAL = "manual"`, `AUTOMATIC = "automatic"`, `classify_ruleset(payload: dict[str, Any]) -> str | None`, and `ruleset_payload(profile: str = MANUAL) -> dict[str, Any]`.
- Produces: `WorkerConfig.merge_mode: str`, restricted to `manual|automatic` and defaulting to `manual`.
- Produces: `ReadinessReport.ruleset_profile: str | None` and `ReviewSnapshot.ruleset_profile: str | None`.
- Consumes later: the auto-merge service requires `config.merge_mode == AUTOMATIC` and `snapshot.ruleset_profile == AUTOMATIC`.

- [ ] **Step 1: Prepare the isolated environment and establish the baseline**

Run:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

Expected: the pre-change suite exits `0`.

- [ ] **Step 2: Write failing config and Ruleset classification tests**

Add tests equivalent to:

```python
def test_worker_merge_mode_defaults_to_manual(tmp_path: Path) -> None:
    config = load_worker_config(write_worker_config(tmp_path, merge_mode=None))
    assert config.merge_mode == "manual"


@pytest.mark.parametrize("value", ["auto", "yes", "", "AUTOMATIC"])
def test_worker_merge_mode_rejects_unknown_values(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError, match="merge_mode"):
        load_worker_config(write_worker_config(tmp_path, merge_mode=value))


def test_ruleset_classifier_distinguishes_manual_and_automatic() -> None:
    manual = ruleset_payload("manual")
    automatic = ruleset_payload("automatic")
    assert classify_ruleset(manual) == "manual"
    assert classify_ruleset(automatic) == "automatic"
    automatic["bypass_actors"].append(
        {"actor_id": 777, "actor_type": "Integration", "bypass_mode": "always"}
    )
    assert classify_ruleset(automatic) is None
```

Extend the assisted-merge fixture so both valid profiles produce `gates.allowed is True`, while a hybrid (`review_count=0`, `require_last_push_approval=True`) produces the existing unsafe Ruleset blocker.

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_worker_config.py tests/test_repository_onboarding.py tests/test_assisted_merge.py -k 'merge_mode or ruleset_profile or classifier'
```

Expected: failures show the missing merge mode, classifier, and snapshot profile fields.

- [ ] **Step 4: Implement the profile module and config parsing**

Create `merge_policy.py` with this public behavior:

```python
MANUAL = "manual"
AUTOMATIC = "automatic"
MERGE_MODES = frozenset({MANUAL, AUTOMATIC})


def ruleset_payload(profile: str = MANUAL) -> dict[str, Any]:
    if profile not in MERGE_MODES:
        raise ValueError(f"unknown Ruleset profile: {profile}")
    automatic = profile == AUTOMATIC
    return {
        "name": "Codex Worker Default Branch",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "bypass_actors": [
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}
        ],
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "update"},
            {
                "type": "pull_request",
                "parameters": {
                    "allowed_merge_methods": ["squash"],
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": False,
                    "require_last_push_approval": not automatic,
                    "required_approving_review_count": 0 if automatic else 1,
                    "required_review_thread_resolution": True,
                },
            },
        ],
    }


def classify_ruleset(payload: dict[str, Any]) -> str | None:
    for profile in (MANUAL, AUTOMATIC):
        expected = ruleset_payload(profile)
        if ruleset_security_fields(payload) == ruleset_security_fields(expected):
            return profile
    return None
```

`ruleset_security_fields` must compare name, target, enforcement, default-branch condition, exact bypass actors, required rule types, squash-only merge, stale-review dismissal, last-push setting, review count, and thread resolution. It must return no profile for any Integration bypass.

Parse the Worker field as:

```python
merge_mode = raw.get("merge_mode", MANUAL)
if merge_mode not in MERGE_MODES:
    raise ConfigError("merge_mode must be 'manual' or 'automatic'")
```

Add `merge_mode = "manual"` to the template.

- [ ] **Step 5: Route onboarding and review through the classifier**

Replace the single `_ruleset_valid` boolean implementation with `classify_ruleset`. Keep onboarding-created Rulesets manual by default. Return the profile from repository status and assisted review; append the unsafe Ruleset blocker only when classification returns `None`.

- [ ] **Step 6: Run focused tests and commit**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_worker_config.py tests/test_repository_onboarding.py tests/test_assisted_merge.py
```

Expected: all selected tests pass.

Commit:

```bash
git add src/codex_mac_worker/merge_policy.py src/codex_mac_worker/config.py src/codex_mac_worker/repository_onboarding.py src/codex_mac_worker/assisted_merge.py templates/worker.toml.example tests/test_worker_config.py tests/test_repository_onboarding.py tests/test_assisted_merge.py
git commit -m "Add single-owner merge policy profiles"
```

### Task 2: Delegation Path Ownership

**Files:**
- Create: `src/codex_mac_worker/coordination.py`
- Modify: `src/codex_mac_worker/control.py`
- Modify: `tests/test_commands_cli.py`

**Interfaces:**
- Produces: `paths_overlap(left: Iterable[str], right: Iterable[str]) -> bool`.
- Produces: `active_task_conflicts(github: Any, repo: str, allowed_paths: Iterable[str]) -> tuple[str, ...]`, returning conflicting Issue URLs.
- Changes: `create_task` rejects a proposed task before Issue creation when its allowed paths overlap a nonterminal Worker Issue.

- [ ] **Step 1: Write failing overlap and create-task tests**

Cover exact, parent/child, normalized trailing slash, rename-style dual paths, non-overlap, and terminal Issues:

```python
@pytest.mark.parametrize(
    ("left", "right"),
    [
        (("src/profile/",), ("src/profile/Profile.vue",)),
        (("src/profile/Profile.vue",), ("src/profile/Profile.vue",)),
        (("src/",), ("src",)),
    ],
)
def test_paths_overlap_by_repository_prefix(left, right) -> None:
    assert paths_overlap(left, right) is True


def test_create_task_rejects_active_path_owner(tmp_path: Path) -> None:
    github = FakeGitHub(active_issue(task_body_with_paths("src/profile/")))
    with pytest.raises(ValueError, match="conflicts with active Worker task"):
        create_task(github, "owner/repo", None, spec_with_paths(tmp_path, "src/profile/Profile.vue"))
    assert github.created_issues == []
```

Terminal labels are exactly `codex:completed` and `codex:cancelled`; `queued`, `claimed`, `running`, `verifying`, `retrying`, `awaiting-review`, `merging`, and `needs-attention` retain path ownership until completion or cancellation.

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_commands_cli.py -k 'overlap or path_owner'
```

Expected: imports or assertions fail because coordination checks do not exist.

- [ ] **Step 3: Implement normalized prefix ownership**

Use POSIX repository paths only. Normalize by stripping `./` and trailing `/`; reject absolute paths, `..`, backslashes, and empty scopes. Two paths overlap when they are equal or either is a slash-delimited ancestor of the other.

`active_task_conflicts` must list open Issues, select those with a nonterminal `codex:` label, parse only valid frozen task blocks, and return their `html_url` values when paths overlap. Malformed active Worker Issues fail closed with a conflict entry instead of being ignored.

Call it from `create_task` after parsing the proposed spec and before `github.create_issue`.

- [ ] **Step 4: Run tests and commit**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_commands_cli.py
```

Expected: all command/control tests pass.

Commit:

```bash
git add src/codex_mac_worker/coordination.py src/codex_mac_worker/control.py tests/test_commands_cli.py
git commit -m "Reject overlapping delegated task paths"
```

### Task 3: Bounded Current-Main Integration Refresh

**Files:**
- Modify: `src/codex_mac_worker/gitops.py`
- Modify: `src/codex_mac_worker/store.py`
- Modify: `src/codex_mac_worker/protocol.py`
- Modify: `src/codex_mac_worker/worker.py`
- Test: `tests/test_gitops.py`
- Test: `tests/test_store.py`
- Test: `tests/test_worker_service.py`
- Test: `tests/test_protocol.py`

**Interfaces:**
- Produces: `IntegrationResult(task_commit: str, integrated_base: str, delivery_head: str, refresh_count: int)`.
- Produces: `GitOperations.integrate_default(worktree, mirror, base_branch, integrated_base, task_paths, *, author_name, author_email) -> IntegrationResult`.
- Extends delivery checkpoints with `task_commit_sha`, `integrated_base_sha`, and `integration_refreshes` using backward-compatible SQLite migration.
- Extends delivery metadata with optional `integrated_base` and `task_commit`; old PR bodies default both to `context_commit` and `delivery_commit` respectively.

- [ ] **Step 1: Write failing Git integration tests**

Create local bare-repository cases proving:

```python
def test_integrate_default_merges_advanced_non_overlapping_main(tmp_path: Path) -> None:
    repo = IntegrationRepository(tmp_path)
    task_commit = repo.commit_task("feature.txt")
    new_main = repo.advance_main("docs.md")
    result = repo.operations.integrate_default(
        repo.worktree,
        repo.mirror,
        "main",
        repo.context,
        ("feature.txt",),
        author_name="Codex Mac Worker",
        author_email="codex-worker@users.noreply.github.com",
    )
    assert result.task_commit == task_commit
    assert result.integrated_base == new_main
    assert len(repo.operations.commit_parents(repo.worktree, result.delivery_head)) == 2


def test_integrate_default_rejects_overlapping_main_change(tmp_path: Path) -> None:
    repo = IntegrationRepository(tmp_path)
    repo.commit_task("feature.txt")
    repo.advance_main("feature.txt")
    with pytest.raises(GitError, match="overlap"):
        repo.integrate()
```

Also test unchanged main (no new commit), merge conflict abort/clean worktree, non-ancestor main, and a third refresh refusal.

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_gitops.py -k 'integrate_default'
```

Expected: failures show the missing integration API.

- [ ] **Step 3: Implement Git integration**

The implementation must:

```python
current_base = rev_parse(mirror, f"refs/heads/{base_branch}")
if current_base == integrated_base:
    return IntegrationResult(task_commit, integrated_base, current_head, refresh_count)
require_ancestor(integrated_base, current_base)
main_paths = changed_paths(mirror, integrated_base, current_base)
if paths_overlap(task_paths, main_paths):
    raise GitError("default branch changes overlap task paths")
if refresh_count >= 2:
    raise GitError("default branch advanced more than two times")
git_merge_no_ff(worktree, current_base, worker_identity)
return IntegrationResult(task_commit, current_base, new_head, refresh_count + 1)
```

On merge failure run `git merge --abort`, verify the worktree is clean at the pre-merge head, then raise a non-retryable `GitError`.

- [ ] **Step 4: Add checkpoint and protocol migration tests**

Prove an old database gains the three columns without losing rows, new checkpoints round-trip the fields, checkpoint identity cannot change, and old delivery YAML without integration keys still parses.

Run:

```bash
.venv/bin/python -m pytest -q tests/test_store.py tests/test_protocol.py -k 'integration or checkpoint or delivery'
```

Expected before implementation: missing-column/field failures.

- [ ] **Step 5: Integrate refresh into Worker delivery**

After the Worker creates the task commit and before saving the final delivery checkpoint:

1. fetch/update the mirror with the existing GitHub installation token;
2. compute task paths from `context_commit..task_commit`;
3. call `integrate_default`;
4. validate the diff against `integrated_base`;
5. rerun the selected repository verification when a merge commit was added;
6. save the refreshed verification and integration identities;
7. build the delivery block with the refreshed exact head.

Before auto-merge, repeat the refresh check. If it adds another integration commit, update the existing Draft PR body, push the new head normally, and rerun all verification. Cap the total at two.

- [ ] **Step 6: Run focused tests and commit**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_gitops.py tests/test_store.py tests/test_protocol.py tests/test_worker_service.py
```

Expected: all selected tests pass.

Commit:

```bash
git add src/codex_mac_worker/gitops.py src/codex_mac_worker/store.py src/codex_mac_worker/protocol.py src/codex_mac_worker/worker.py tests/test_gitops.py tests/test_store.py tests/test_protocol.py tests/test_worker_service.py
git commit -m "Refresh Worker deliveries against current main"
```

### Task 4: Durable Exact-Head Worker Auto-Merge

**Files:**
- Create: `src/codex_mac_worker/automatic_merge.py`
- Modify: `src/codex_mac_worker/store.py`
- Modify: `src/codex_mac_worker/durable_github.py`
- Modify: `src/codex_mac_worker/github.py`
- Modify: `src/codex_mac_worker/worker.py`
- Test: `tests/test_automatic_merge.py`
- Test: `tests/test_store.py`
- Test: `tests/test_durable_github.py`

**Interfaces:**
- Produces: `AutomaticMergeResult(repo, issue_number, pr_number, approved_head, merge_commit_sha, merged)`.
- Produces: `automatic_merge_task(github, store, reference, *, pr_number, expected_head, merge_mode) -> AutomaticMergeResult`.
- Produces EventStore methods `begin_auto_merge`, `get_auto_merge`, and `complete_auto_merge` keyed by repo/Issue/PR/task hash/head.
- Adds durable operations `mark_pull_request_ready` and `merge_pull_request(expected_head=...)`.

- [ ] **Step 1: Write failing gate and idempotency tests**

Cover:

```python
def test_auto_merge_requires_both_trusted_signals(tmp_path: Path) -> None:
    github = AutoMergeGitHub(profile="automatic")
    with pytest.raises(AutoMergeBlocked, match="local merge mode"):
        automatic_merge_task(github, store(tmp_path), REF, pr_number=44,
                             expected_head="c" * 40, merge_mode="manual")
    assert github.writes == []


def test_auto_merge_marks_ready_rechecks_and_squashes(tmp_path: Path) -> None:
    github = AutoMergeGitHub(profile="automatic")
    result = automatic_merge_task(github, store(tmp_path), REF, pr_number=44,
                                  expected_head="c" * 40, merge_mode="automatic")
    assert result.merged is True
    assert github.writes == ["ready", "merge"]
    assert github.merge_payload == {"merge_method": "squash", "sha": "c" * 40}


def test_auto_merge_reconciles_lost_success_without_second_merge(tmp_path: Path) -> None:
    github = AutoMergeGitHub(merge_succeeds_then_raises=True)
    operation_store = store(tmp_path)
    with pytest.raises(TransientGitHubError):
        automatic_merge_task(github, operation_store, REF, pr_number=44,
                             expected_head="c" * 40, merge_mode="automatic")
    result = automatic_merge_task(github, operation_store, REF, pr_number=44,
                                  expected_head="c" * 40, merge_mode="automatic")
    assert result.merged is True
    assert github.merge_calls == 1
```

Also cover changed head before Ready, changed head after Ready, wrong author, malformed App metadata, manual Ruleset, Integration bypass, unresolved thread, failed checks, high-risk delivery, and remote merge with an unexpected head.

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_automatic_merge.py tests/test_durable_github.py -k 'auto_merge or merge_pull_request or mark_pull_request_ready'
```

Expected: missing module and durable-operation failures.

- [ ] **Step 3: Add the durable SQLite operation**

Create `auto_merge_operations` with immutable identity columns, state, attempts, last error, merge commit, created/updated/completed timestamps, and a unique `(repo, issue_number, pr_number, task_hash, expected_head)` key. `begin_auto_merge` must be transactional and reject identity drift.

- [ ] **Step 4: Add durable Ready and merge writes**

`DurableGitHub._deliver` must reconcile before each write:

```python
if operation == "mark_pull_request_ready":
    pull = remote.get_pull_request(repo, pr_number)
    return pull if pull.get("draft") is False else remote.mark_pull_request_ready(repo, pr_number)

if operation == "merge_pull_request":
    pull = remote.get_pull_request(repo, pr_number)
    if pull.get("merged_at"):
        if pull["head"]["sha"].lower() != expected_head:
            raise ValueError("merged PR head differs from expected head")
        return {"merged": True, "sha": pull.get("merge_commit_sha", "")}
    return remote.merge_pull_request(repo, pr_number, expected_head=expected_head)
```

The outbox idempotency key includes the exact head. A changed head creates no write and blocks before enqueue.

- [ ] **Step 5: Implement the separate automatic merge function**

The function must first reconcile an existing merged PR, then call `review_task`, require `merge_mode == automatic`, require `snapshot.ruleset_profile == automatic`, require exact PR number/head and `gates.allowed`, record the operation, mark Ready durably, review again, and merge durably. It never creates a GitHub review and never calls the human `merge_task` path.

- [ ] **Step 6: Run focused tests and commit**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_automatic_merge.py tests/test_store.py tests/test_durable_github.py tests/test_assisted_merge.py
```

Expected: all selected tests pass.

Commit:

```bash
git add src/codex_mac_worker/automatic_merge.py src/codex_mac_worker/store.py src/codex_mac_worker/durable_github.py src/codex_mac_worker/github.py src/codex_mac_worker/worker.py tests/test_automatic_merge.py tests/test_store.py tests/test_durable_github.py
git commit -m "Add durable Worker exact-head auto-merge"
```

### Task 5: Daemon Reconciliation and Existing PR Adoption

**Files:**
- Modify: `src/codex_mac_worker/repository_onboarding.py`
- Modify: `src/codex_mac_worker/worker.py`
- Modify: `src/codex_mac_worker/daemon.py`
- Modify: `tests/test_daemon.py`
- Modify: `tests/test_worker_service.py`

**Interfaces:**
- Adds lifecycle label `codex:merging` with description `Verified Worker PR is being auto-merged`.
- Produces: `WorkerService.auto_merge_delivery(repository, issue, task) -> str`, returning `completed`, `merging`, or `needs-attention`.
- Changes: `WorkerDaemon.process_review_tasks` adopts `awaiting-review` tasks when local automatic mode and automatic Ruleset are both present.

- [ ] **Step 1: Write failing daemon state-machine tests**

Prove:

```python
def test_daemon_auto_merges_existing_awaiting_review_task(tmp_path: Path) -> None:
    daemon, store, github, service = automatic_daemon(tmp_path)
    store.upsert_task(repo="owner/repo", issue_number=12, task_hash="h",
                      state="awaiting-review", pr_number=44)
    service.auto_merge_result = "completed"
    assert daemon.process_review_tasks() is True
    assert store.get_task("owner/repo", 12)["state"] == "completed"
    assert github.closed_issue == 12


def test_daemon_manual_mode_leaves_delivery_awaiting_review(tmp_path: Path) -> None:
    daemon, store, github, service = manual_daemon(tmp_path)
    assert daemon.process_review_tasks() is False
    assert service.auto_merge_calls == 0
```

Also test `merging` crash resume, transient failure retained for at most two attempts, permanent failure to `needs-attention`, merged readback completion, and no second Codex run.

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_daemon.py tests/test_worker_service.py -k 'auto_merge or merging or awaiting_review'
```

Expected: current daemon never calls auto-merge and tests fail.

- [ ] **Step 3: Implement lifecycle transitions**

After Draft PR creation:

- manual mode records `awaiting-review` exactly as today;
- automatic mode records `merging`, updates the status comment, and invokes the automatic merge service;
- service completion writes `codex:completed` and closes the Issue only after merge readback;
- retryable failure preserves `merging` with attempt/error evidence;
- permanent failure writes `needs-attention` without changing the PR head;
- daemon startup scans both `merging` and eligible legacy `awaiting-review` tasks.

Legacy adoption must use the existing task, checkpoint, branch, and PR number. It must not invoke the runner or create a PR.

- [ ] **Step 4: Run tests and commit**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_daemon.py tests/test_worker_service.py tests/test_worker_discovery.py
```

Expected: all selected tests pass.

Commit:

```bash
git add src/codex_mac_worker/repository_onboarding.py src/codex_mac_worker/worker.py src/codex_mac_worker/daemon.py tests/test_daemon.py tests/test_worker_service.py tests/test_worker_discovery.py
git commit -m "Reconcile verified deliveries through auto-merge"
```

### Task 6: MacBook Principal-Agent Skill and Operations Documentation

**Files:**
- Modify: `skills/dispatch-codex-task/SKILL.md`
- Modify: `/Users/qiaoz-macair/.codex/skills/dispatch-codex-task/SKILL.md` during deployment only
- Modify: `docs/OPERATIONS.md`
- Modify: `docs/MACBOOK_SETUP.md`
- Modify: `docs/MAC_MINI_SETUP.md`
- Modify: `README.md`
- Test: `tests/test_dispatch_skill.py`

**Interfaces:**
- The repository skill becomes the source installed on MacBook by `scripts/install_macbook.sh`.
- `--yes` is allowed only for a strict subtask of an already authorized parent objective after clean-context and conflict checks.
- Manual `codexctl task merge` remains documented for repositories using manual mode.

- [ ] **Step 1: Add skill contract assertions**

Add a text-level test that requires the skill to contain all of these concepts and forbids the old absolute prohibition:

```python
required = (
    "principal development agent",
    "strict subset of the authorized parent objective",
    "active path ownership",
    "codexctl task create --yes",
    "merge_mode = \"automatic\"",
    "Codex Goal",
)
for phrase in required:
    assert phrase in skill_text
assert "Run only after explicit confirmation of that final specification" not in skill_text
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dispatch_skill.py
```

Expected: the current skill lacks the new role and autonomous delegation contract.

- [ ] **Step 3: Rewrite the bounded delegation section**

Document two dispatch paths:

1. owner explicitly asks to publish a standalone task: preview and confirm the final spec;
2. MacBook agent delegates inside an authorized parent development task: verify subset, clean pushed context, low/medium risk, non-overlapping paths, then use `--yes` without another owner prompt.

State that the MacBook may keep the work local and that the Mac mini cannot further delegate. Replace “Worker never merges” documentation with the two-signal automatic policy while preserving manual mode instructions and all production exclusions.

- [ ] **Step 4: Run documentation tests and commit**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_dispatch_skill.py
git diff --check
```

Expected: tests and whitespace checks pass.

Commit:

```bash
git add skills/dispatch-codex-task/SKILL.md docs/OPERATIONS.md docs/MACBOOK_SETUP.md docs/MAC_MINI_SETUP.md README.md tests/test_dispatch_skill.py
git commit -m "Define MacBook principal-agent delegation workflow"
```

### Task 7: Full Verification, Review, Publish, and Rollout

**Files:**
- Verify all files changed in Tasks 1-6.
- Deploy repository skill to `/Users/qiaoz-macair/.codex/skills/dispatch-codex-task/` through `scripts/install_macbook.sh`.
- Deploy Worker to `/Users/qiaoz-macmini/Library/Application Support/CodexWorker/` from the reviewed merge commit.
- Modify trusted Mac mini config: `/Users/qiaoz-macmini/Library/Application Support/CodexWorker/config/worker.toml` by setting `merge_mode = "automatic"`.

**Interfaces:**
- Produces a reviewed codex-mac-worker PR and exact merge commit.
- Produces a running Mac mini Worker with automatic mode.
- Produces automatic reconciliation and squash merge of EaseWise PR #13 when all gates pass.

- [ ] **Step 1: Run the complete local verification**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests
mkdir -p /tmp/codex-mac-worker-auto-merge-wheel
.venv/bin/pip wheel --no-deps . --wheel-dir /tmp/codex-mac-worker-auto-merge-wheel
git diff --check origin/main...HEAD
git status --short
```

Expected: zero test failures, compile/build exit `0`, one wheel is created, no whitespace errors, and the worktree is clean.

- [ ] **Step 2: Request independent code review**

Review the exact `origin/main..HEAD` range against the approved design. Fix every Critical and Important issue with a fresh failing test, rerun the full suite, and obtain a final `Ready to merge: Yes` assessment.

- [ ] **Step 3: Publish the system PR**

Push `codex/single-owner-auto-merge` and create a Draft PR to `qiaozhang1225/codex-mac-worker:main`. The PR body must list architecture changes, migration behavior, security boundaries, tests, and the exact EaseWise PR #13 rollout.

- [ ] **Step 4: Merge and deploy as one approved rollout**

After the user approves the exact implementation PR/head, squash merge it. On Mac mini:

```bash
SYSTEM_PR=$(gh pr view codex/single-owner-auto-merge --repo qiaozhang1225/codex-mac-worker --json number --jq .number)
APPROVED_HEAD=$(gh pr view "$SYSTEM_PR" --repo qiaozhang1225/codex-mac-worker --json headRefOid --jq .headRefOid)
test "$APPROVED_HEAD" = "$(git rev-parse HEAD)"
gh pr merge "$SYSTEM_PR" --repo qiaozhang1225/codex-mac-worker --squash --match-head-commit "$APPROVED_HEAD"
REVIEWED_MERGE_COMMIT=$(gh pr view "$SYSTEM_PR" --repo qiaozhang1225/codex-mac-worker --json mergeCommit --jq .mergeCommit.oid)
test -n "$REVIEWED_MERGE_COMMIT"

git -C "$HOME/codex-mac-worker" fetch origin main
test "$(git -C "$HOME/codex-mac-worker" rev-parse origin/main)" = "$REVIEWED_MERGE_COMMIT"
git -C "$HOME/codex-mac-worker" merge --ff-only "$REVIEWED_MERGE_COMMIT"
"$HOME/Library/Application Support/CodexWorker/venv/bin/pip" install --no-deps --force-reinstall "$HOME/codex-mac-worker"
```

Back up SQLite before installation, run `--check-config` with manual mode, restart and verify the PID, then set the trusted config to `merge_mode = "automatic"`, check config again, and restart once more. Do not rerun the full installer because plist/templates do not require replacement unless their diff changed.

- [ ] **Step 5: Install the MacBook skill and execute the live drill**

Run `scripts/install_macbook.sh` from the reviewed merge commit, verify the installed skill hash matches the repository source, and observe the Mac mini:

- existing Issue #12 remains the same task hash;
- existing PR #13 remains the same PR and Codex delivery head until any bounded integration refresh;
- no new Codex run or duplicate PR appears;
- automatic gate recognizes the current single-owner Ruleset;
- Worker marks the PR ready, exact-head squash merges it, confirms the merge commit, closes Issue #12, and applies `codex:completed`;
- Worker PID remains stable and outbox has no pending item.

- [ ] **Step 6: Verify the test-environment handoff**

Record the EaseWise merge commit and downstream test-environment pipeline state. Stop at test-environment observation; do not deploy production and do not auto-revert.
