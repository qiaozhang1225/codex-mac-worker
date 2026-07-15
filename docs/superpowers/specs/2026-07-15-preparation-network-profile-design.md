# Preparation 网络权限隔离设计

## 背景

Worker 将仓库内已审查的 preparation 命令通过 `codex sandbox -P codex-worker` 执行。现有 `codex-worker` 权限配置明确关闭网络，因此全新 worktree 中的 `pip install` 和 `npm ci` 无法访问依赖源，任务会在 Codex 启动前进入 `needs-attention`。

## 方案

新增 `codex-worker-preparation` 权限配置，继承现有 `codex-worker` 文件系统约束，仅打开域名白名单网络：

- `pypi.org`
- `files.pythonhosted.org`
- `registry.npmjs.org`

`run_commands` 增加显式 `permission_profile` 参数，默认仍为 `codex-worker`。首次执行和 revise 流程中的 preparation 调用显式使用 `codex-worker-preparation`；verification 继续使用默认无网络配置。Codex 执行入口不改变。

该方案遵循 Codex permission profile 的最小权限与域名白名单模型。准备命令仍在 macOS Seatbelt sandbox 中，不能读取主机凭据或仓库中的 `.env` 文件。

## 验收与恢复

测试必须证明自定义 profile 会传给 `codex sandbox -P`，Worker 的 preparation 使用新 profile，verification 仍使用 `codex-worker`，模板只允许三个依赖域名。部署后先在保留 worktree 中执行受限联网冒烟测试，再取消 Issue #7 并按相同冻结规格重新派发；不恢复已失败会话。
