# 单 Slot Scheduled 冒烟运行手册

本手册用于验证 Mac mini 上一条完整、可审计的 Codex App Scheduled 执行链。首次演练只启用 `Dual Mac Slot 1`；`Dual Mac Slot 2` 和 `Dual Mac Slot 3` 必须保持暂停。演练不修改 Scheduled 定义、Codex App 内部文件或数据库，也不部署任何环境。

## 前置条件

开始前逐项确认：

1. Mac mini 保持开机、不睡眠，Codex App 正在运行，并能访问目标仓库及 GitHub。
2. Scheduled 任务名严格为 `Dual Mac Slot 1`。任务保存的明确 model 与 reasoning effort 是本次演练的事实来源；记录其当前值，不模拟或推断默认值。
3. `Dual Mac Slot 1` 已启用；`Dual Mac Slot 2`、`Dual Mac Slot 3` 均为暂停状态。
4. `repositories.toml` 已通过 `duomac-config-validate`，目标仓库路径、Git remote 和 GitHub 登录状态有效。
5. MacBook 已发布一个经用户明确确认的 schema v2 低风险 Issue。Issue body 必须完整声明 revision、冻结 context commit、context files、allowed paths、有序 milestones、verification profile 和 `direct-main` delivery mode。
6. Issue 唯一的 `duomac:*` 状态标签是 `duomac:ready`，且目标路径不与任何 `duomac:active` 任务重叠。

## 触发方式

在 Codex App 中手工触发 `Dual Mac Slot 1` 一次，观察本次运行创建的可见任务。不要启用另外两个 Slot，也不要在终端启动 `codex exec`、守护进程、LaunchDaemon 或第二个执行器。

Scheduled 运行应先校验本机配置，再用任务名尾部的 `1` 作为 slot，带显式 claim 标志调用 `duomac-scheduled-pick`。一次运行最多领取一个 Issue，且必须按 picker JSON 的 `outcome` 分支，不得按 `reason` 推断执行权限。

## Picker 结果分支

- `clean-noop`：报告原样 `reason` 和 `maintenance_actions: []`，不执行代码，并在 Codex App 支持时归档本次非执行运行。
- `maintenance`：报告原样 `reason` 及每一项 `maintenance_actions`，不执行代码，并在 Codex App 支持时归档本次非执行运行。
- `preview` 或 `error`：报告结果并停止，不执行代码。
- `claimed`：报告原样 `maintenance_actions`，只执行返回 Issue 的当前完整 schema v2 合同。只有这个结果允许进入代码执行。

## `claimed` 的证据链

以下证据必须按顺序出现：

1. **`duomac:ready`**：领取前，Issue 为 open，body 是唯一当前任务合同，标签为 `duomac:ready`。
2. **Scheduled `task-start`**：picker 原子领取后，Issue 切换为 `duomac:active`，并新增结构化 `task-start`。事件应包含当前 revision、context/base/skill commit、`execution_mode: scheduled`、`slot: 1` 和唯一 `claim_id`。
3. **逐里程碑 checkpoint**：执行器从冻结 context commit 读取适用的仓库 `AGENTS.md` 和 Issue 声明的每个 context file，再建立隔离 `codex/*` 工作树。里程碑必须从 1 开始按合同顺序执行；每个里程碑完成后先重新读取 Issue body，再发布同 revision 的结构化 checkpoint，之后才可开始下一里程碑。Checkpoint 是证据，不是审批门。
4. **`direct-main` delivery**：最后一个里程碑 checkpoint 已存在后，重新读取 Issue body，运行 Git preflight 和 `.duomac/project.toml` 中选择的 verification profile。通过后使用正常、非强制的 direct-main 流程交付；若远端前进，只允许按协议进行一次无冲突 rebase，并重新执行完整选定验证。
5. **`duomac:completed`**：交付提交已存在于远端默认分支后，发布结构化 delivery 证据，Issue 切换为 `duomac:completed` 并关闭。证据应列出实际 commit、changed paths、逐条 acceptance results、准确验证命令和 remaining risks。

## 停止条件

出现以下任一情况，当前 Slot 必须保留安全现场、发布结构化 `blocked` 事件并停止；`Dual Mac Slot 2` 或 `Dual Mac Slot 3` 不得接管、恢复或复制执行：

- 冻结提交中的适用 `AGENTS.md` 或任一 Issue 声明 context file 缺失、不可读；
- Issue revision 变化，且新的完整替代合同无法重新校验或使已完成工作失效；
- 实际改动超出 allowed paths、触及 protected paths，或与远端变更发生路径重叠；
- preflight、选定 verification profile、rebase 或正常 push 失败，且无法在原合同范围内安全修复；
- 需要新的产品决定、改变 delivery mode、扩大范围、部署、生产数据、force push 或另一个执行器。

评论只能提供证据或建议，不能扩展 Issue body 的授权。不得自行创建替代 Issue，也不得通过修改 Scheduled 定义或 Codex App 内部存储绕过停止条件。

## 演练结束条件

演练仅在以下条件全部满足时通过：

- Codex App 中可查看本次 `Dual Mac Slot 1` Scheduled 运行；
- GitHub Issue 中存在同一 revision 的连续证据：`duomac:ready` → Scheduled `task-start` → 每个 milestone checkpoint → direct-main delivery → `duomac:completed`；
- 远端默认分支包含交付 commit，Issue 已关闭；
- 仓库改动只包含 Issue 允许的路径，verification profile 通过；
- `Dual Mac Slot 2` 和 `Dual Mac Slot 3` 始终保持暂停；
- 没有部署，没有 force push，没有创建新 Issue，也没有创建、修改或启停任何 Scheduled 任务。

任一条件不满足时，本次演练不得记为通过；以 Issue 中最后一个结构化事件作为当前状态证据。
