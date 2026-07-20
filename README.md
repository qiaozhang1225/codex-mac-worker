# Dual-Mac Codex Collaboration

This repository provides one versioned Codex skill for coordinating development between MacBook and Mac mini through GitHub Issues. MacBook remains the principal development device and sole dispatcher. Mac mini may execute a complete, bounded task in an interactive Codex App conversation or an independent Codex App Scheduled run.

## Roles

- **MacBook** develops directly, explores product and technical decisions, prepares PRD/Project Card/Spec context, decides with the user whether to delegate, and is the only device that formally publishes task Issues.
- **Mac mini** claims at most one confirmed Issue per run, executes its frozen schema v2 contract, records every milestone checkpoint, and delivers within the approved Git and path boundaries. It never creates task Issues or expands scope.

Task duration does not determine delegation. Delegate when product decisions are closed, context is committed and pushed, acceptance and paths are explicit, and the execution plan can continue without repeated product judgment. Every formal Issue creation still requires explicit user confirmation after the final contract is shown.

## Install an exact revision

Install the same repository commit on both Macs:

```bash
git clone https://github.com/qiaozhang1225/codex-mac-worker.git
cd codex-mac-worker
git checkout <approved-full-commit-sha>
./scripts/install_skill.sh --remove-legacy-client
```

The installer creates a small Python 3.12 environment, installs PyYAML, validates and atomically installs `dual-mac-collaboration` into Codex skills, writes `.source-commit`, and adds `duomac-*` command wrappers under `~/.local/bin`. It installs the approved example as `~/Library/Application Support/DualMacCollaboration/repositories.toml.example`; it never creates or overwrites `repositories.toml`. It requires authenticated `gh` and does not start a daemon.

## Interactive use

On MacBook, ask Codex App:

> 使用 dual-mac-collaboration 判断这个计划是否适合交给 Mac mini；先展示最终任务契约，不要在我确认前创建 Issue。

On Mac mini, ask Codex App:

> 使用 dual-mac-collaboration，从指定仓库读取一个 duomac:ready 任务，校验后在可见对话中开始执行。

Interactive Mac mini pickup begins only when the user opens or directs that Codex App conversation. Each schema v2 milestone gets a checkpoint before the next milestone; checkpoints are evidence and do not require MacBook approval. The final milestone checkpoint must precede delivery.

## Codex App Scheduled use

Scheduled execution uses three independent tasks named exactly `Dual Mac Slot 1`, `Dual Mac Slot 2`, and `Dual Mac Slot 3`. Each task runs the same tracked prompt in `skills/dual-mac-collaboration/assets/scheduled-slot-prompt.md`; the task name supplies its slot number. A run validates local configuration, atomically claims at most one non-overlapping `duomac:ready` Issue, and continues in that same visible Scheduled task. A no-op creates no task and makes no mutation. Slots never resume one another's failed work.

Copy the tracked example to `~/Library/Application Support/DualMacCollaboration/repositories.toml`, then keep these two Mac-local entries:

```toml
schema_version = 1
max_parallel_tasks = 3
poll_interval_minutes = 10

[[repositories]]
github = "qiaozhang1225/EaseWise"
local_path = "/Users/qiaoz-macmini/EaseWise"

[[repositories]]
github = "qiaozhang1225/codex-mac-worker"
local_path = "/Users/qiaoz-macmini/codex-mac-worker"
```

Validate configuration and preview selection before enabling the production claim flag:

```bash
duomac-config-validate --config "$HOME/Library/Application Support/DualMacCollaboration/repositories.toml"
duomac-scheduled-pick --help
```

See the official [Codex Scheduled tasks documentation](https://developers.openai.com/codex/app/automations) for task creation, testing, permissions, and run management.

The helpers are preview-first:

```bash
duomac-issue-create --repo OWNER/REPO --spec task.md
duomac-issue-create --repo OWNER/REPO --spec task.md --yes
duomac-git-preflight --help
duomac-git-deliver --help
```

## Delivery modes

`direct-main` is the default for one-owner, non-overlapping work. It permits one non-conflicting rebase, reruns full configured verification, and then performs a normal push to the default branch. `task-branch` pushes only the `codex/*` task branch for later integration. Neither mode permits force push.

## Deliberate exclusions

The active design has no Goal mode, `codex exec`, external daemon or LaunchDaemon Worker, GitHub App identity gate, mandatory pull request, mandatory approval, or Ruleset gate. Only Codex App Scheduled owns recurring pickup. It does not deploy production, force push, let Mac mini expand scope, or let Mac mini create Issues. GitHub Issues preserve the current contract and evidence; interactive conversations and Scheduled runs remain visible to the user.

The final unattended implementation is preserved at Git tag `legacy-worker-v0.1.0`. Historical source and operating documents can be inspected from that tag without keeping two runnable protocols on the default branch.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

The approved design and implementation plan are under `docs/superpowers/`.
