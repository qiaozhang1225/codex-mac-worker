# Codex App Scheduled 双 Mac 执行设计

## 状态

- 日期：2026-07-20
- 状态：口头设计已确认，等待书面规格复核
- 目标：让 Mac mini 使用 Codex App 内置 Scheduled 功能，自动领取多个仓库中的已确认 GitHub Issue，并在最多三个独立任务对话中安全并行执行
- 适用仓库：`qiaozhang1225/EaseWise`、`qiaozhang1225/codex-mac-worker`

## 背景

当前 `dual-mac-collaboration` 只允许用户在 Mac mini 的可见 Codex App 对话中手工领取任务，并明确禁止后台轮询。第一次真实 README 任务已经完成，但暴露了两个缺口：

1. Issue 计划包含两个里程碑，执行记录只有 milestone 1 checkpoint，完成脚本仍允许直接发布 delivery 并关闭 Issue。
2. 用户希望 GitHub 发布 `duomac:ready` 后，由 Mac mini 的 Codex App 自动创建可见任务并执行，而不是依赖人工新建对话或恢复旧 Worker。

Codex App 的 Scheduled 任务可以定时运行、为独立运行创建新的任务对话、调用技能，并在 Scheduled 页面保留结果。项目涉及本地文件时，Mac mini 必须保持开机、桌面 App 运行且项目路径可访问。参考：[Codex Scheduled tasks](https://developers.openai.com/codex/app/automations)。

## 已确认原则

- 继续使用 GitHub Issue 作为唯一版本化任务契约和跨设备审计链。
- MacBook 仍是唯一派单端；Issue 创建继续要求用户看到完整契约后单独确认。
- Mac mini 可通过 Codex App 的独立 Scheduled 任务自动领取和执行，无需逐次批准。
- 不使用 Goal 模式、`codex exec`、旧 Worker、LaunchDaemon 或自建常驻轮询进程。
- Scheduled 运行必须在 Codex App 中可查看；GitHub Issue 继续记录 task-start、checkpoint、blocked 和 delivery。
- Mac mini 最多同时执行三个任务。允许并行的前提是仓库和路径边界不冲突。
- 不授予部署、生产数据或生产凭据；现有低/中风险、明确路径和验收条件边界不变。

## 总体架构

Mac mini 创建三个独立的 Codex App Scheduled 执行槽：

- `Dual Mac Slot 1`
- `Dual Mac Slot 2`
- `Dual Mac Slot 3`

三个槽每 10 分钟运行一次，并错开约一分钟。每次 Scheduled 运行本身就是一个新的 Codex 任务对话；有合格 Issue 时在该对话中完成领取和执行，没有合格 Issue 时作为 no-op 结束。

当前已验证的 Scheduled 创建/更新接口要求为模型与推理强度保存明确值，不支持“默认/未指定”哨兵。三个槽在没有确定性任务分流规则时应使用相同的明确配置，避免同一任务因随机被不同槽领取而出现执行质量差异。模型与推理强度只能通过受支持的 Scheduled 控制修改，不直接编辑 Codex App 内部文件或数据库。

Codex App 负责：

- 定时触发；
- 创建并显示 Scheduled 任务对话；
- 运行模型、技能和本地工具；
- 保存运行状态与历史。

`dual-mac-collaboration` 负责：

- 读取 Mac mini 本地仓库配置；
- 查询、校验和领取 `duomac:ready` Issue；
- 控制最大并发和路径冲突；
- 建立独立 worktree；
- 执行当前 Issue 的完整计划；
- 强制 checkpoint 完整性、验证和 Git 交付。

Scheduled 功能是唯一调度器。本地配置、锁和辅助脚本只在 Scheduled 任务启动后运行，不构成长驻服务。

## Mac mini 本地配置

配置与运行状态位于：

```text
~/Library/Application Support/DualMacCollaboration/
├── repositories.toml
├── dispatch.lock
└── claims/
```

`repositories.toml` 的首版结构为：

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

安装时必须检查真实路径和 Git remote；示例路径不是绕过校验的依据。以后新增仓库只更新本地配置，不要求修改技能。配置不得包含 GitHub Token、私钥、环境变量值或部署凭据。

## Scheduled 领取算法

新增确定性命令 `duomac-scheduled-pick`。写操作默认 preview，只有显式确认标志才执行领取。

每个 Scheduled 槽执行以下流程：

1. 读取并校验 `repositories.toml`。
2. 获取全部配置仓库的 `duomac:ready` 和 `duomac:active` Issue。
3. 获取本机跨槽原子锁；在锁内重新读取 GitHub 状态，避免两个槽领取同一 Issue。
4. 若 active 总数已经达到 `max_parallel_tasks`，返回 no-op。
5. 解析 active 契约并建立每个仓库的 `allowed_paths` 占用表。
6. 按 Issue 创建时间从早到晚检查 ready 候选：
   - 必须使用当前支持的任务 schema；
   - 本地路径必须存在且 remote 与配置仓库一致；
   - context commit 必须存在并满足祖先规则；
   - context commit 中的 `.duomac/project.toml` 必须有效；
   - 同仓库 `allowed_paths` 不得与任何 active 任务重叠；
   - 风险、验证 profile 和保护路径必须满足现有规则。
7. 对最早的合格任务写 `task-start` 并切换为 `duomac:active`。
8. 在 task-start 成功后释放领取锁，并由当前 Scheduled 对话继续建立 worktree 和执行。

不同仓库默认不构成路径冲突。同一仓库的路径相同、父子目录重叠或其中一方允许整个父目录时均视为冲突。路径暂时冲突的 ready Issue 保持 ready，等待后续运行，不标记 blocked。

领取以当前 revision 的唯一 `task-start` 事件作为权威证据，状态标签只是投影。领取工具先在锁内确认不存在同 revision 的 task-start，再发布带唯一 `claim_id` 的 task-start，最后切换 active 标签。若评论成功而标签更新失败，后续 picker 只修复标签，不再创建任务；若评论失败，则保持 ready。`claims/` 只保存本机诊断记录，不能覆盖 GitHub 事件事实。

## 并发与 Git 交付

- 最多允许三个 `duomac:active` Issue。
- 每个 Issue 使用独立 `codex/*` 分支和 worktree。
- 每个 Scheduled 运行最多领取一个 Issue。
- 同一仓库的并行任务必须拥有不重叠的 `allowed_paths`。
- `direct-main` 交付时若默认分支前进且路径不重叠，允许一次 rebase，之后重新运行完整选定验证 profile。
- 默认分支出现路径重叠、rebase 冲突或刷新后再次拒绝 push 时，记录 blocked 并停止。
- 永不 force push，永不让一个 Scheduled 任务把工作再次委派给其他执行器。

## 任务契约 schema v2

新任务使用 `schema_version: 2`。执行计划由自由字符串列表改成有序里程碑：

```yaml
execution_plan:
  - milestone: 1
    objective: 完成项目概览、目录地图和本地开发说明
    steps:
      - 对照冻结提交核对仓库结构
      - 修改 README 并运行内容检查
  - milestone: 2
    objective: 完成验证和 staging 部署导航
    steps:
      - 增加验证及部署章节
      - 检查链接、范围并运行 fast profile
```

规则：

- `milestone` 必须从 1 开始连续递增。
- `objective` 必须是单一、非空结果。
- `steps` 必须是非空字符串列表，且完整覆盖从实现到验证和交付前准备的连续执行过程。
- 验证命令仍只来自 `.duomac/project.toml`，不得写入 Issue 后成为新的执行权限。
- 已关闭的 schema v1 Issue 保持可读且不回写。新 Issue 只创建 v2。
- Scheduled 槽遇到 ready 状态的 v1 Issue 时不得猜测里程碑；将其标记 blocked，并要求 MacBook 发布完整 v2 修订。

## Checkpoint 完整性

`issue_checkpoint.py` 和 `issue_complete.py` 增加对 Issue 事件历史的确定性校验：

- task-start 之后，第一个 checkpoint 只能是 milestone 1。
- checkpoint 必须严格按 1 到 N 顺序写入，不能重复或跳号。
- 每个 checkpoint 必须与当前 Issue revision 一致。
- revision 更新后，旧 revision 的 checkpoint 不计入新 revision 的完成集合。
- delivery 前必须存在当前 revision 的完整 `1...N` checkpoint。
- 任一 acceptance result 为 `not-met`、缺少 checkpoint、存在未解决 blocked 状态或 revision 不一致时，完成脚本拒绝发布 delivery 和关闭 Issue。
- 最后一个 milestone checkpoint 与 delivery 是两个独立事件；delivery 不能替代 checkpoint。

task-start 增加执行来源审计字段：

```yaml
execution_mode: scheduled
slot: 1
claim_id: 40-character-lowercase-hex-id
```

手工领取时使用 `execution_mode: interactive`，不提供 `slot`。

## 失败与恢复

- 没有 ready Issue、并发已满或所有候选路径冲突：返回 no-op，不修改 Issue 或仓库。
- GitHub 网络、认证或限流错误：不领取；当前运行报告错误并结束，由下次 Scheduled 运行重新检查。
- 契约、项目配置、context commit 或 local path 非法：写 blocked 事件并标记 `duomac:blocked`。
- task-start 之后执行失败：当前对话保存安全现场并写 blocked；其他 Slot 不接管 active Issue。
- active 任务长期没有进展：其他 Slot 可报告异常，但不得自动 resume、retry 或复制执行。
- Codex App 是否在当前版本同时调度三个 Scheduled 任务必须通过 Mac mini 实测。若 App 将它们串行化，安全规则仍成立，实际并发量只是低于三。
- 只有确认 `maintenance_actions: []` 的 `clean-noop` 运行自动归档当前对话；`maintenance`、`preview`、`error`、`claimed` 和 blocked 运行全部保留可见。归档使用 Codex App 支持的当前对话归档操作，不修改内部文件或数据库。

## 技能与仓库改动

计划增加或修改：

```text
skills/dual-mac-collaboration/
├── SKILL.md
├── references/
│   ├── issue-protocol.md
│   ├── checkpoints.md
│   └── scheduled-execution.md
├── scripts/
│   ├── duomac_contracts.py
│   ├── duomac_github.py
│   ├── issue_checkpoint.py
│   ├── issue_complete.py
│   ├── config_validate.py
│   └── scheduled_pick.py
└── assets/
    └── scheduled-slot-prompt.md
```

同时更新安装脚本、命令包装器、技能元数据、仓库 README、协议测试、命令测试、安装测试和压力场景。安装器只创建缺失的配置目录或模板，不覆盖用户已有的 `repositories.toml`，也不直接创建 Codex App Scheduled 对象。

## 权限边界

Scheduled 任务使用 Codex App 的 local 执行环境。Mac mini 必须允许其访问两个配置仓库并使用 GitHub 网络连接。权限只覆盖：

- 读取 GitHub Issue 和评论；
- 更新双 Mac 状态标签和结构化评论；
- 在配置仓库中创建 worktree、提交和正常 push；
- 运行 `.duomac/project.toml` 中的验证命令。

不得向 Scheduled 任务提供部署凭据、生产环境凭据、GitHub App 私钥或旧 Worker secrets。不得因为 Scheduled 任务无人值守而放宽 Issue risk、allowed paths、protected paths 或 delivery mode 规则。

## 测试策略

实施使用测试驱动方式，先建立以下失败用例：

- completion 在缺少任一 milestone checkpoint 时必须失败；
- checkpoint 重复、跳号、乱序或 revision 过期时必须失败；
- 三个并发 picker 只能领取一次同一 Issue；
- active 达到 3 时不得领取；
- 同仓库路径重叠时跳过候选，不同仓库可并行；
- invalid v1 ready Issue 转入 blocked；
- 网络错误不得留下半领取状态；
- installer 不覆盖现有本地仓库配置；
- Scheduled Prompt 明确调用技能、使用 slot、限制一次领取一个任务且禁止 Goal、部署和 force push。

测试通过后再更新技能正文和辅助脚本，并运行完整 pytest、官方 skill validator、安装器隔离测试和 Mac mini 实机 smoke test。

## Mac mini 上线步骤

1. 将通过测试的技能提交推送到 `qiaozhang1225/codex-mac-worker`。
2. Mac mini 拉取精确提交并重新运行 `scripts/install_skill.sh --remove-legacy-client`。
3. 写入并验证两个仓库的真实 GitHub 名称、本地路径和 remote。
4. 验证 `gh auth status`、Codex App 对两个仓库的文件权限以及 GitHub 网络访问。
5. 手工运行一次 `duomac-scheduled-pick` preview，确认不会产生写操作。
6. 在 Mac mini Codex App 中创建三个独立 Scheduled 任务，频率均为 10 分钟并错开约一分钟。
7. 核对三个 Slot 实际保存的明确模型与推理强度；首次测试允许保留已保存值，且不为追求“默认”而修改 App 内部文件。
8. 先启用 Slot 1；确认空运行行为正常后，创建一个低风险测试 Issue，手工触发 Slot 1 的 Prompt，完成一次领取、checkpoint 和 direct-main 交付。
9. 单 Slot 测试通过后，再启用 Slot 2、Slot 3。
10. 发布两个路径不重叠的测试 Issue，验证形成两个可见并行任务；再发布一个路径重叠任务，确认其保持 ready。
11. 保持 Mac mini 开机、系统不睡眠且 Codex App 持续运行。

## 验收标准

- MacBook 创建并确认的 schema v2 Issue 能由 Mac mini Scheduled 任务自动领取。
- 每个已领取 Issue 对应可查看的 Scheduled 任务对话和 GitHub task-start。
- 两个配置仓库均可被领取，新增仓库只需修改 Mac mini 本地配置。
- 不冲突任务最多三个并行；同一 Issue 不会被两个 Slot 重复领取。
- 路径冲突任务保持 ready，等待冲突解除。
- completion 无法跳过任何当前 revision 里程碑 checkpoint。
- no-op、网络失败、非法契约和执行失败遵守各自的无副作用或 blocked 行为。
- 两台 Mac 安装的技能 `.source-commit` 一致。
- 系统中没有旧 Worker、`codex exec`、LaunchDaemon、Goal 模式或静默常驻轮询。

## 非目标

- 不让 Mac mini 创建或修订正式任务契约。
- 不自动执行 high-risk、部署、生产数据、数据库迁移或不可逆任务。
- 不自动 merge PR；当前默认交付仍是受保护的正常 `direct-main`。
- 不恢复 GitHub App 精确身份校验、强制 Ruleset 或旧 Worker 状态机。
- 不保证 Codex App 未公开或未验证的跨任务 API；并行能力通过三个独立 Scheduled 任务实现并在实机验证。
