# Codex Mac Worker

A dual-Mac development system: the MacBook Codex agent is the principal development agent and may develop directly or delegate a bounded subtask to one always-on macOS Worker. The Worker consumes GitHub Issue tasks, runs `codex exec` in isolated Git worktrees, validates the diff and repository-approved tests, opens a Draft PR, and can squash-merge its own exact verified head under an explicit single-owner policy.

It deliberately excludes Codex Goal/“目标” mode, cloud execution, desktop scheduled tasks, production deployment, automatic rollback, high-risk work, and multiple competing Workers. Automatic merge is repository-specific and requires two trusted signals: local `merge_mode = "automatic"` plus the recognized automatic Ruleset profile.

## Components

- `codex-worker`: LaunchDaemon process for polling, execution, recovery, verification, and Draft PR delivery.
- `codexctl`: MacBook CLI for repository onboarding, task creation/control, read-only review, and manual exact-head merge approval where manual mode is selected.
- `skills/dispatch-codex-task`: MacBook principal-agent skill for choosing local work or delegating a strict subset of an authorized objective.
- `scripts/`: macOS installation, diagnosis, maintenance, repository-label bootstrap, and uninstall tools.
- `templates/`: Worker configuration and LaunchDaemon templates.

The Worker uses a dedicated `CODEX_HOME` permission profile. It can write only in the active worktree, cannot read the rest of the user home directory, and has no network access. System toolchain files remain readable so Python and Node runtimes can execute. Repository-level `.codex/config.toml` files are rejected in v1 so they cannot override that boundary.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Set up the dispatch device with [docs/MACBOOK_SETUP.md](docs/MACBOOK_SETUP.md), then follow [docs/MAC_MINI_SETUP.md](docs/MAC_MINI_SETUP.md) on the always-on Worker. Day-to-day commands and failure handling are in [docs/OPERATIONS.md](docs/OPERATIONS.md); the trust boundaries are in [docs/SECURITY.md](docs/SECURITY.md).
