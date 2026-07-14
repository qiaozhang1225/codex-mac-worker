# Codex Mac Worker

A single macOS Worker that consumes bounded GitHub Issue tasks, runs `codex exec` in isolated Git worktrees, validates the diff and repository-approved tests, then opens a Draft PR. The MacBook can review and perform one approval-bound squash merge; the Worker itself never merges.

It deliberately excludes Codex Goal/“目标” mode, cloud execution, desktop scheduled tasks, automatic merge or approval for any future PR, production deployment, and multiple competing Workers.

## Components

- `codex-worker`: LaunchDaemon process for polling, execution, recovery, verification, and Draft PR delivery.
- `codexctl`: MacBook CLI for repository onboarding, task creation/control, read-only review, and explicit one-head merge approval.
- `skills/dispatch-codex-task`: optional MacBook skill that prepares a bounded specification and requires human confirmation.
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
