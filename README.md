# Dual-Mac Codex Collaboration

This repository provides one versioned Codex skill for coordinating development between two visible Codex App sessions through GitHub Issues. It is designed for a single owner who wants the MacBook to remain the principal development device while using an always-available Mac mini for complete, bounded tasks.

## Roles

- **MacBook** develops directly, explores product and technical decisions, prepares PRD/Project Card/Spec context, decides with the user whether to delegate, and is the only device that formally publishes task Issues.
- **Mac mini** visibly fetches a confirmed Issue in Codex App, executes its complete plan, records structured checkpoints, and delivers within the approved Git and path boundaries.

Task duration does not determine delegation. Delegate when product decisions are closed, context is committed and pushed, acceptance and paths are explicit, and the execution plan can continue without repeated product judgment. Every formal Issue creation still requires explicit user confirmation after the final contract is shown.

## Install an exact revision

Install the same repository commit on both Macs:

```bash
git clone https://github.com/qiaozhang1225/codex-mac-worker.git
cd codex-mac-worker
git checkout <approved-full-commit-sha>
./scripts/install_skill.sh --remove-legacy-client
```

The installer creates a small Python 3.12 environment, installs PyYAML, atomically installs `dual-mac-collaboration` into Codex skills, writes `.source-commit`, and adds `duomac-*` command wrappers under `~/.local/bin`. It requires authenticated `gh` and does not start a background service.

## Visible use

On MacBook, ask Codex App:

> 使用 dual-mac-collaboration 判断这个计划是否适合交给 Mac mini；先展示最终任务契约，不要在我确认前创建 Issue。

On Mac mini, ask Codex App:

> 使用 dual-mac-collaboration，从指定仓库读取一个 duomac:ready 任务，校验后在可见对话中开始执行。

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

The active design has no Goal mode, unattended daemon, GitHub App identity gate, mandatory pull request, mandatory approval, or Ruleset gate. It does not poll Issues, run silently, deploy production, or let Mac mini expand scope. GitHub Issues preserve the current contract and evidence; the Codex App conversations remain visible to the user.

The final unattended implementation is preserved at Git tag `legacy-worker-v0.1.0`. Historical source and operating documents can be inspected from that tag without keeping two runnable protocols on the default branch.

## Development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

The approved design and implementation plan are under `docs/superpowers/`.
