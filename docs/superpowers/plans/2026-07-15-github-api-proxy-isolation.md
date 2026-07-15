# GitHub API Proxy Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure the Mac mini Worker can reach GitHub before login or while Clash Verge is unavailable by preventing only its GitHub HTTP clients from inheriting system proxies.

**Architecture:** The existing `GitHubAppAuth` and `GitHubClient` remain the only owners of GitHub HTTP connections. Both construct `httpx.Client` with `trust_env=False`; no global environment, Git, Codex CLI, task-state, retry, or outbox behavior changes.

**Tech Stack:** Python 3.12, httpx 0.28, pytest 9, GitHub CLI, macOS LaunchDaemon, SSH.

## Global Constraints

- Only GitHub App token and GitHub REST/GraphQL clients bypass system and environment proxies.
- Codex CLI, Git, and other Worker subprocesses retain their existing network behavior.
- Existing error classification, retry limits, task state, and durable outbox semantics remain unchanged.
- Existing SQLite, outbox, logs, worktrees, and Issue #5 state must not be manually edited or deleted.
- Issue #5 recovery requires a separate explicit control-operation confirmation after deployment verification.

---

### Task 1: Make both GitHub clients ignore environment proxies

**Files:**
- Modify: `tests/test_github.py`
- Modify: `src/codex_mac_worker/github.py`

**Interfaces:**
- Consumes: `httpx.Client(*, base_url: str, transport: BaseTransport | None, timeout: int, trust_env: bool)`.
- Produces: unchanged `GitHubAppAuth` and `GitHubClient` public interfaces; both internal clients use `trust_env=False`.

- [ ] **Step 1: Write the failing regression test**

Add this test to `tests/test_github.py`:

```python
def test_github_http_clients_ignore_environment_proxies(
    monkeypatch, tmp_path: Path
) -> None:
    client_options: list[dict] = []

    class RecordingClient:
        def __init__(self, **kwargs) -> None:
            client_options.append(kwargs)

    monkeypatch.setattr(httpx, "Client", RecordingClient)

    GitHubAppAuth(
        app_id="123",
        installation_id="456",
        private_key_path=tmp_path / "app.pem",
    )
    GitHubClient(token_provider=lambda: "token")

    assert [options["trust_env"] for options in client_options] == [False, False]
```

- [ ] **Step 2: Run the regression test and verify RED**

Run: `.venv/bin/pytest tests/test_github.py::test_github_http_clients_ignore_environment_proxies -v`

Expected: FAIL with `KeyError: 'trust_env'`, proving the clients currently inherit proxy configuration.

- [ ] **Step 3: Add the minimal implementation**

Change both client constructors in `src/codex_mac_worker/github.py` to include:

```python
self._client = httpx.Client(
    base_url=api_url,
    transport=transport,
    timeout=30,
    trust_env=False,
)
```

- [ ] **Step 4: Run the regression test and full suite**

Run:

```bash
.venv/bin/pytest tests/test_github.py::test_github_http_clients_ignore_environment_proxies -v
.venv/bin/pytest
git diff --check
```

Expected: focused test passes, all tests pass, and `git diff --check` exits with no output.

- [ ] **Step 5: Commit the implementation**

```bash
git add tests/test_github.py src/codex_mac_worker/github.py
git commit -m "fix: isolate GitHub API from system proxy"
```

### Task 2: Publish the reviewed Worker change

**Files:**
- No additional source files.

**Interfaces:**
- Consumes: branch `codex/github-api-ignore-system-proxy` with passing tests.
- Produces: a GitHub pull request against `qiaozhang1225/codex-mac-worker:main`.

- [ ] **Step 1: Verify the branch diff and test evidence**

Run `git status --short`, `git diff --check origin/main...HEAD`, and `.venv/bin/pytest`.

Expected: clean working tree, no whitespace errors, and all tests passing.

- [ ] **Step 2: Push and open the PR**

Push branch `codex/github-api-ignore-system-proxy` and open a PR titled `fix: isolate GitHub API from system proxy` against `main`. Stop before merge until the user explicitly approves it.

### Task 3: Upgrade and verify the Mac mini Worker

**Files:**
- Update the installed package under `/Users/qiaoz-macmini/Library/Application Support/CodexWorker/venv/` from the merged remote `main`.
- Do not edit `/Users/qiaoz-macmini/Library/Application Support/CodexWorker/state/worker.sqlite3`.

**Interfaces:**
- Consumes: an explicitly approved and merged PR commit on `origin/main`.
- Produces: a restarted `com.easewise.codex-worker` service running the merged source.

- [ ] **Step 1: Record the pre-upgrade state**

Use SSH to record the running PID, installed source, repository commit, database task states, and outbox counts without printing credentials.

- [ ] **Step 2: Upgrade from the exact merged commit**

Pull the Worker source on Mac mini, verify the merged SHA, install it into the existing virtual environment, and restart the LaunchDaemon without modifying configuration or secrets.

- [ ] **Step 3: Verify proxy isolation and health**

Confirm the installed clients use `trust_env=False`, GitHub App token/API access succeeds, and the LaunchDaemon remains running without new proxy errors.

- [ ] **Step 4: Verify state preservation**

Confirm Issue #5, task state, failed outbox rows, logs, and any worktree evidence remain present and were not manually rewritten.

### Task 4: Recover the bounded frontend task

**Files:**
- No direct file edits.

**Interfaces:**
- Consumes: healthy upgraded Worker and existing EaseWise Issue #5.
- Produces: either a controlled retry or a replacement Issue, selected only after inspecting the supported transition.

- [ ] **Step 1: Inspect the supported recovery transition**

Read command handling and Issue #5 state to determine whether `retry` is valid for this classified infrastructure failure. Do not post a command yet.

- [ ] **Step 2: Present the exact operation for confirmation**

Show the repository, Issue URL, command type, reason, expected transition, and whether it creates a new Issue. Obtain explicit confirmation.

- [ ] **Step 3: Execute and monitor the confirmed operation**

Post only the confirmed idempotent command, then monitor Issue labels, Worker logs, branch creation, run records, verification, and Draft PR creation. Stop if scope or security gates reject the task.
