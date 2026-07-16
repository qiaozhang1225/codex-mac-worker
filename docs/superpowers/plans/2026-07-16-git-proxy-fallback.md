# Git Proxy Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Worker Git network operations prefer an explicitly configured HTTP(S) proxy, fall back to a clean direct route, and remain bounded to three total attempts.

**Architecture:** Extend `WorkerConfig` with an optional validated `git_proxy_url`, then pass it only into `GitOperations`. `GitOperations._git_network` alternates proxy/direct/proxy while reusing the existing transient/permanent classifier and retry delays; `_git` removes inherited proxy variables for direct attempts.

**Tech Stack:** Python 3.12, `tomllib`, `urllib.parse`, `subprocess`, pytest, TOML Worker configuration.

## Global Constraints

- Do not use Codex Goal mode.
- The Mac mini value is exactly `http://127.0.0.1:7897`.
- Only Git network commands may receive the proxy setting.
- GitHub API clients, Codex execution, preparation commands, prompts, and logs remain unchanged.
- Proxy URLs must use `http` or `https`, include a host, and contain no username or password.
- The attempt sequence is proxy, direct, proxy with three total attempts.
- Permanent Git failures stop after the first attempt.
- Installation tokens remain only in the existing temporary Askpass environment.
- Retrying EaseWise Issue #12 is outside this implementation and requires separate approval.

---

### Task 1: Parse and expose a safe optional Git proxy

**Files:**
- Modify: `src/codex_mac_worker/config.py`
- Modify: `src/codex_mac_worker/cli.py`
- Modify: `tests/test_worker_config.py`
- Modify: `tests/test_operational_assets.py`
- Modify: `templates/worker.toml.example`

**Interfaces:**
- Produces: `WorkerConfig.git_proxy_url: str | None`
- Consumes: a top-level optional TOML key `git_proxy_url`
- Produces: `GitOperations(..., proxy_url=config.git_proxy_url)` at Worker startup

- [ ] **Step 1: Write failing configuration tests**

Extend `write_worker_config` with a `proxy` fragment and add these cases:

```python
def test_worker_config_parses_safe_git_proxy(tmp_path: Path) -> None:
    config_path = tmp_path / "worker.toml"
    write_worker_config(
        config_path,
        tmp_path,
        discovery="discover_installation_repositories = true",
        proxy='git_proxy_url = "http://127.0.0.1:7897"',
    )

    config = load_worker_config(config_path)

    assert config.git_proxy_url == "http://127.0.0.1:7897"


def test_worker_config_treats_empty_git_proxy_as_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "worker.toml"
    write_worker_config(
        config_path,
        tmp_path,
        discovery="discover_installation_repositories = true",
        proxy='git_proxy_url = ""',
    )

    assert load_worker_config(config_path).git_proxy_url is None


@pytest.mark.parametrize(
    "proxy",
    [
        "socks5://127.0.0.1:7897",
        "http://user:secret@127.0.0.1:7897",
        "http://127.0.0.1:not-a-port",
    ],
)
def test_worker_config_rejects_unsafe_git_proxy(tmp_path: Path, proxy: str) -> None:
    config_path = tmp_path / "worker.toml"
    write_worker_config(
        config_path,
        tmp_path,
        discovery="discover_installation_repositories = true",
        proxy=f'git_proxy_url = "{proxy}"',
    )

    with pytest.raises(ConfigError, match="git_proxy_url"):
        load_worker_config(config_path)
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_config.py -k git_proxy -q
```

Expected: failures because `write_worker_config` and `WorkerConfig` do not yet expose `git_proxy_url`.

- [ ] **Step 3: Implement minimal safe parsing**

Add the field at the end of `WorkerConfig` so existing positional test fixtures remain compatible:

```python
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    # existing fields remain unchanged
    git_proxy_url: str | None = None


def _worker_proxy_url(raw: dict[str, Any]) -> str | None:
    value = raw.get("git_proxy_url")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("git_proxy_url must be an HTTP(S) URL without credentials")
    value = value.strip()
    if not value:
        return None
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError as exc:
        raise ConfigError("git_proxy_url must contain a valid port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("git_proxy_url must be an HTTP(S) URL without credentials")
    return value.rstrip("/")
```

Populate `git_proxy_url=_worker_proxy_url(raw)` in `load_worker_config`.

- [ ] **Step 4: Wire and expose the setting without widening its scope**

Change only Worker Git construction:

```python
git = GitOperations(
    cache_root=config.cache_root,
    worktree_root=config.worktree_root,
    proxy_url=config.git_proxy_url,
)
```

Add `"git_proxy_url": config.git_proxy_url` to `--check-config` JSON. Add
`git_proxy_url = ""` to `templates/worker.toml.example`, and assert the rendered template
loads it as `None` in `test_templates_are_valid_and_example_config_loads`.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_worker_config.py tests/test_operational_assets.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the configuration unit**

```bash
git add src/codex_mac_worker/config.py src/codex_mac_worker/cli.py \
  tests/test_worker_config.py tests/test_operational_assets.py \
  templates/worker.toml.example
git commit -m "feat: configure safe Git proxy"
```

---

### Task 2: Alternate bounded Git network routes

**Files:**
- Modify: `src/codex_mac_worker/gitops.py`
- Modify: `tests/test_gitops.py`

**Interfaces:**
- Consumes: `GitOperations(proxy_url: str | None = None)`
- Produces: `_proxy_environment(use_proxy: bool) -> dict[str, str | None]`
- Changes: `_git(..., env: Mapping[str, str | None] | None)` removes keys mapped to `None`
- Preserves: `_git_network` returns `CompletedProcess[str]` and raises bounded `GitError`

- [ ] **Step 1: Write the failing proxy/direct/proxy route test**

```python
def test_proxy_network_retries_alternate_proxy_direct_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = GitOperations(
        cache_root=tmp_path / "cache",
        worktree_root=tmp_path / "worktrees",
        proxy_url="http://127.0.0.1:7897",
        network_retry_delays=(0.1, 0.2),
        sleep=lambda _: None,
    )
    environments: list[object] = []

    def fake_git(cwd: Path, *args: str, env: object = None, check: bool = True):
        environments.append(env)
        if len(environments) < 3:
            return subprocess.CompletedProcess(
                ["git", *args], 128, "", "fatal: Failed to connect to github.com"
            )
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(operations, "_git", fake_git)
    operations._git_network(tmp_path, "fetch")

    assert environments[0]["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert environments[1]["HTTPS_PROXY"] is None
    assert environments[2]["HTTPS_PROXY"] == "http://127.0.0.1:7897"
```

- [ ] **Step 2: Write failing isolation and permanent-error tests**

```python
def test_direct_route_removes_inherited_proxy_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = GitOperations(cache_root=tmp_path, worktree_root=tmp_path)
    monkeypatch.setenv("HTTPS_PROXY", "http://inherited.invalid:1")

    def fake_run(*args: object, **kwargs: object):
        assert "HTTPS_PROXY" not in kwargs["env"]
        return subprocess.CompletedProcess(["git"], 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    operations._git(tmp_path, "status", env={"HTTPS_PROXY": None})


def test_permanent_failure_does_not_fall_back_from_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = GitOperations(
        cache_root=tmp_path,
        worktree_root=tmp_path,
        proxy_url="http://127.0.0.1:7897",
    )
    environments: list[object] = []

    def fake_git(cwd: Path, *args: str, env: object = None, check: bool = True):
        environments.append(env)
        return subprocess.CompletedProcess(
            ["git", *args], 128, "", "fatal: Authentication failed"
        )

    monkeypatch.setattr(operations, "_git", fake_git)
    with pytest.raises(GitError, match="Authentication failed"):
        operations._git_network(tmp_path, "fetch")

    assert len(environments) == 1
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_gitops.py -k 'proxy_network or inherited_proxy or fall_back_from_proxy' -q
```

Expected: failures because `proxy_url` and removable environment entries are not implemented.

- [ ] **Step 4: Implement route-specific environments**

Add the constructor parameter and environment constants:

```python
_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def __init__(
    self,
    *,
    cache_root: Path,
    worktree_root: Path,
    git_path: str = "git",
    proxy_url: str | None = None,
    network_retry_delays: tuple[float, ...] = (1.0, 3.0),
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    # existing assignments remain unchanged
    self.proxy_url = proxy_url


def _proxy_environment(self, *, use_proxy: bool) -> dict[str, str | None]:
    value = self.proxy_url if use_proxy else None
    return {key: value for key in _PROXY_ENV_KEYS}
```

Update `_git` so `None` removes inherited variables:

```python
merged_env = os.environ.copy()
if env:
    for key, value in env.items():
        if value is None:
            merged_env.pop(key, None)
        else:
            merged_env[key] = value
```

Update `_git_network` before each call:

```python
attempt_env: dict[str, str | None] = dict(env or {})
if self.proxy_url is not None:
    attempt_env.update(self._proxy_environment(use_proxy=attempt % 2 == 0))
result = self._git(cwd, *args, env=attempt_env, check=False)
```

When `proxy_url` is `None`, do not inject proxy controls so existing direct-only behavior is
unchanged.

- [ ] **Step 5: Run Git tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_gitops.py -q
```

Expected: all Git operation tests pass, including existing bounded retry and authentication
precedence cases.

- [ ] **Step 6: Commit the Git route unit**

```bash
git add src/codex_mac_worker/gitops.py tests/test_gitops.py
git commit -m "fix: fall back between Git proxy and direct routes"
```

---

### Task 3: Document, verify, and publish the Worker change

**Files:**
- Modify: `docs/MAC_MINI_SETUP.md`
- Modify: `docs/OPERATIONS.md`
- Modify: `tests/test_operational_assets.py`

**Interfaces:**
- Documents: `git_proxy_url = "http://127.0.0.1:7897"`
- Preserves: explicit per-PR merge and per-command Issue approval boundaries

- [ ] **Step 1: Write the failing documentation assertion**

In `test_shell_scripts_parse_and_docs_cover_manual_boundaries`, add:

```python
assert "git_proxy_url" in setup
assert "proxy → direct → proxy" in operations
```

- [ ] **Step 2: Run the assertion and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_operational_assets.py::test_shell_scripts_parse_and_docs_cover_manual_boundaries -q
```

Expected: failure because the setting and route order are not documented.

- [ ] **Step 3: Add focused operator documentation**

Add this Mac mini configuration example:

```toml
git_proxy_url = "http://127.0.0.1:7897"
```

Document that the proxy must be a trusted local HTTP(S) CONNECT proxy, credentials are
rejected, an empty value disables it, and the bounded route order is `proxy → direct → proxy`.

- [ ] **Step 4: Run the full verification suite**

Run:

```bash
git diff --check
.venv/bin/python -m pytest -q
```

Expected: no whitespace errors and all tests pass.

- [ ] **Step 5: Commit documentation and verification coverage**

```bash
git add docs/MAC_MINI_SETUP.md docs/OPERATIONS.md tests/test_operational_assets.py
git commit -m "docs: operate Git proxy fallback"
```

- [ ] **Step 6: Request independent review and publish a Draft PR**

Use `superpowers:requesting-code-review` against `origin/main...HEAD`. Fix every Critical or
Important finding with another red-green test cycle. Re-run the full verification suite, push
`codex/git-proxy-fallback`, and create a Draft PR containing the design, route order, test
evidence, security boundaries, and Mac mini deployment steps.

- [ ] **Step 7: Stop at the immutable merge boundary**

Show the Draft PR URL, full head SHA, changed paths, complete test result, review findings, and
deployment delta. Do not merge or deploy until the user explicitly approves that exact PR
snapshot.
