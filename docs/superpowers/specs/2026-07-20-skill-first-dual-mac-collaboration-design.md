# Skill-First 双 Mac Codex App 协作设计

## 背景

现有 `codex-mac-worker` 以无人值守 Worker 为中心：Mac mini 的 LaunchDaemon 轮询 GitHub Issue，使用 GitHub App 获取权限，调用 `codex exec`，通过 SQLite 保存状态，并以 Draft PR、Ruleset profile、精确 bot 身份、PR head 和审批 fingerprint 控制交付与合并。

GitHub 外部策略已经改为极简模式：EaseWise 是不具备 Ruleset 功能的私有仓库，`codex-mac-worker` 仅保留默认分支禁止删除和禁止 force push 的最小 Ruleset。旧代码仍把 GitHub App 身份、readiness attestation、Draft PR 和 recognized Ruleset 当作硬前置，因此 `codexctl repo status qiaozhang1225/EaseWise` 会返回 `blocked`。这不是一条规则的缺陷，而是旧交付模型与新协作方式整体不一致。

截至设计确认时，Mac mini 的旧 Worker 数据库没有活跃任务，适合执行一次完整迁移。

## 目标

把公开的 `codex-mac-worker` 仓库原地转型为两台 Mac 上的 Codex App 共用协作技能，解决两个核心问题：

1. 使用 GitHub Issue 在 MacBook 与 Mac mini 之间进行清晰、可追溯的任务沟通；
2. 保证任务在派发、执行、修订、检查点和交付过程中不丢失信息。

开发全部在两台设备上可见的 Codex App 中执行。MacBook 是主导端，负责产品判断、讨论、技术验证、任务完备性判断和正式派单；Mac mini 只执行边界明确、计划完备的任务。

## 非目标

- 不再运行 `codex exec` 后台 Worker。
- 不保留 Issue 轮询、LaunchDaemon、SQLite 状态机或无人值守恢复。
- 不使用 Codex Goal/“目标”模式。
- 不要求 GitHub App、安装令牌、App 私钥或精确 bot 身份。
- 不要求 Pull Request、Draft PR、审批 fingerprint 或特定 Ruleset profile。
- 不自动部署、操作生产数据、执行数据库迁移或自动回滚。
- 不允许 Mac mini 自行扩大范围、改变产品决定或再次派发任务。
- 不把预计执行时长作为是否派发的判断标准。

## 已锁定决策

### 角色边界

- 用户与 MacBook Codex 共同决定是否派发任务，最终决定权属于用户。
- MacBook Codex 可以建议派发、完善任务规格并生成 Issue，但正式创建前必须展示完整任务并获得用户确认。
- Mac mini 只能领取明确指定给它的任务，不能修改产品目标、扩大允许路径、改变交付模式或再次派发。
- Mac mini 可以在任务契约内决定实现细节，并连续执行长任务。
- 需要产品判断的任务留在 MacBook；是否派发不取决于任务是一小时还是一天。

### 派发资格

任务只有同时满足以下条件时才具备派发资格：

1. 不再需要执行端作新的产品判断；
2. 已经过充分讨论和必要的技术验证；
3. 存在完整、可执行且可以连续推进的计划；
4. 目标、验收条件、允许路径、上下文和验证方式完整；
5. 风险为低或中，不涉及部署、生产数据、不可逆操作或凭据；
6. 与 MacBook 当前开发范围、其他活跃任务及协作者修改不存在已知路径冲突。

### 长任务

长任务按里程碑在同一 Issue 写结构化检查点。只要仍处于任务契约内，Mac mini 写完检查点后继续执行，不等待 MacBook 逐次批准。出现范围扩大、产品歧义、计划失效、冲突或高风险操作时才暂停并交回 MacBook。

## 仓库目标结构

主分支以技能为唯一产品入口：

```text
skills/dual-mac-collaboration/
├── SKILL.md
├── references/
│   ├── roles-and-delegation.md
│   ├── issue-protocol.md
│   ├── checkpoints.md
│   └── git-delivery.md
└── scripts/
    ├── issue_create.py
    ├── issue_validate.py
    ├── issue_checkpoint.py
    └── issue_complete.py
```

脚本是 Codex App 按需调用的确定性辅助工具，不是常驻进程。它们负责结构校验、GitHub Issue 读写和格式化证据，不执行模型任务，不轮询队列，也不自行决定派发。

旧 `codex-worker` 和 `codexctl` 命令不提供兼容模式。可以复用的 Git 范围校验、worktree、远端漂移和证据格式化逻辑应通过测试重新提取到 skill 脚本；GitHub App、readiness、PR 和 merge 状态机不能以旧模块或隐藏开关继续存在。

仓库当前名称在本次迁移中保持不变，避免把代码重构与 GitHub 仓库重命名绑定在一起；未来如需改名，应作为单独的低风险维护任务。

## Skill 安装与版本同步

- 公开仓库的 Git commit 是 skill 版本的唯一来源。
- MacBook 与 Mac mini 都从同一仓库安装 `dual-mac-collaboration`，并记录当前加载的完整 commit SHA。
- MacBook 完成并验证 skill 更新后才推送；Mac mini 只在用户明确要求更新时拉取，不自动更新。
- 每次跨设备任务的 `task-start` 评论记录执行端当前 skill commit，便于排查两端协议差异。
- 更新后应新开 Codex App 对话或刷新技能加载，再运行只读自检确认 Issue schema、标签和项目配置版本一致。
- skill 调用本机已经登录的 `gh` 和 Git 身份，不读取 GitHub App 私钥，也不在仓库中保存个人访问令牌。

## 项目侧配置

每个接入项目使用轻量、可审查的配置：

```text
.duomac/project.toml
```

示例：

```toml
schema_version = 1
default_base_branch = "main"
protected_paths = [".env", "product/deploy"]
max_changed_files = 30
max_diff_lines = 3000

[verification.fast]
commands = ["项目已有的快速验证命令"]

[verification.full]
commands = ["项目已有的完整验证命令"]
```

项目配置只保存稳定的 Git 与验证规则，不包含 GitHub App ID、Installation ID、Ruleset profile、merge mode、运行时超时或后台服务设置。Issue 选择 `verification_profile`，验证命令来自项目配置。

## Issue 是唯一任务协议

### 唯一指令来源

Issue 正文是当前任务契约的唯一权威位置。评论只能记录过程、问题、修订原因和交付证据，不能直接改变目标、范围、验收条件或交付模式。

其他协作者可以在评论中提出建议；需要采纳时，由用户或 MacBook Codex 更新正文。

### 正文结构

```yaml
schema_version: 1
revision: 1

role:
  dispatcher: macbook
  executor: mac-mini

objective: 单一、明确的交付目标

context:
  commit: 完整的 40 位 commit SHA
  files:
    - 已提交的 PRD、Spec、设计或技术验证文件
  decisions:
    - 已确定且执行端不得自行改变的决定

acceptance:
  - 可客观验证的条件

scope:
  allowed_paths:
    - 允许修改的最小路径
  out_of_scope:
    - 明确不处理的内容

execution_plan:
  - 完整、连续的执行步骤

verification_profile: fast
delivery_mode: direct-main
risk: low
```

`delivery_mode` 必须是 `direct-main` 或 `task-branch`。默认使用 `direct-main`；多人并行、冲突概率较高或需要单独集成时由 MacBook 选择 `task-branch`。

### 修订

- Issue 正文始终保存最新、完整的任务，而不是在多条评论中拼接增量要求。
- 每次改变任务契约时，`revision` 必须递增。
- 更新正文后追加一条简短评论，说明修改原因和受影响字段。
- Mac mini 在开工、每个里程碑和最终交付前重新读取正文。
- revision 变化后禁止按旧版本交付。若新版本仍可执行，Mac mini 记录已读取的新 revision 并继续；若已完成工作或计划被新版本否定，则进入 blocked 并说明影响。

不使用任务哈希、GitHub App attestation 或 PR fingerprint。信息一致性来自单一权威正文、递增 revision 和结构校验。

## 状态模型

使用独立命名空间，避免旧 Worker 标签继续产生语义影响：

```text
duomac:ready
duomac:active
duomac:blocked
duomac:delivered
duomac:completed
duomac:cancelled
```

- `ready`：已由用户确认并正式派发，等待 Mac mini 可见领取。
- `active`：Mac mini 已读取当前 revision 并开始执行。
- `blocked`：无法在当前任务契约内继续，需要 MacBook 决策或外部状态变化。
- `delivered`：`task-branch` 已推送并记录证据，但尚未进入默认分支。
- `completed`：交付 commit 已进入默认分支并通过规定验证。
- `cancelled`：用户或 MacBook 明确取消，不再继续。

旧 `codex:*` 标签只保留在历史 Issue 中作为审计记录，不再驱动任何程序。

## 双端工作流

### MacBook 派单

1. 在当前项目中读取 PRD、Project Card、Spec、设计、Git 状态和活跃 Issue。
2. 判断任务是否仍需要产品决策，是否已经充分讨论，是否存在完备连续计划。
3. 检查上下文文件已 commit、上下文 SHA 可从远端获取、允许路径不与本地或其他活跃任务重叠。
4. 生成完整 Issue 正文，选择验证 profile 和 delivery mode。
5. 向用户展示最终规格并获得明确确认。
6. 创建 Issue 并添加 `duomac:ready`。

技能不能根据“看起来适合”自动发布，也不能把用户对设计的确认当作发布确认。

### Mac mini 领取与执行

1. 用户在 Mac mini 的 Codex App 中明确要求获取任务。
2. skill 查询指定仓库的 `duomac:ready` Issue；没有后台轮询或静默启动。
3. 校验正文完整性、revision、项目配置、context commit、风险、允许路径和验证 profile。
4. 校验通过后添加 `duomac:active` 并写 `task-start` 评论。
5. 从最新远端状态创建 `codex/<issue>-<slug>` 临时分支和独立 worktree。
6. 在 Codex App 对话中可见地执行计划。
7. 每完成一个里程碑写 checkpoint，然后继续。
8. 最终检查 changed paths、文件数、diff 行数、敏感文件、二进制文件、revision 和项目验证结果。
9. 根据 delivery mode 交付并写 delivery 评论。

### 开工记录

```yaml
type: task-start
revision: 1
skill_commit: 完整 SHA
base_commit: 完整 SHA
plan_summary:
  - 当前执行阶段
```

### 检查点

```yaml
type: checkpoint
revision: 1
milestone: 2
completed:
  - 已完成内容
commits:
  - 完整 commit SHA；尚未提交时为空
verification:
  - 已执行命令及结果
scope_status: within-scope
next:
  - 下一步
blockers: []
```

### 最终交付

```yaml
type: delivery
revision: 1
delivery_mode: direct-main
commit: 完整 SHA
changed_paths:
  - 实际修改路径
acceptance_results:
  - criterion: 验收条件
    status: met
    evidence: 对应测试或检查证据
verification:
  - 命令及结果
remaining_risks: []
```

## Git 操作设计

### 保留的能力

- 派单前检查上下文是否 commit 和 push。
- 获取远端状态并验证 context commit 与默认分支关系。
- 使用临时分支和 worktree 隔离任务。
- 校验允许路径、受保护路径、文件数量和 diff 行数。
- 在提交前检查敏感文件、二进制文件和异常大 diff。
- 记录 Issue、revision、commit、测试和验收条件之间的对应关系。
- 禁止 force push。
- 在远端变化时进行明确、可回退的冲突处理。

这些是通用 Git 安全实践，不依赖旧 Worker 的 GitHub 权限模型。

### `direct-main`

开发始终在临时任务分支/worktree 中完成。最终推送前重新获取 `origin/main`：

1. 若远端 main 与开始执行时一致，运行最终验证后执行非强制 `git push origin HEAD:main`。
2. 若远端 main 已前进但修改路径不重叠，允许把任务分支 rebase 到最新 main 一次；随后重新执行全部验证和范围检查，再非强制推送。
3. 若 rebase 冲突、路径重叠、验证失败或 push 再次遇到远端漂移，停止并标记 `duomac:blocked`。
4. 永远不 force push，不覆盖他人提交。

成功进入 main 后写 delivery、标记 `duomac:completed` 并关闭 Issue。

### `task-branch`

Mac mini 推送 `codex/<issue>-<slug>`，写 delivery 并标记 `duomac:delivered`，Issue 保持打开。MacBook Codex 根据当时仓库状态选择 fast-forward、cherry-pick、merge 或 PR；本设计不强制其中任何一种。进入默认分支并复核验证后才标记 `completed` 和关闭 Issue。

## 异常处理

- **正文不完整**：不领取，标记 blocked 并列出缺失字段。
- **revision 变化**：完成当前安全检查点，重读正文；禁止发布旧 revision 的结果。
- **本地无关修改**：使用独立 worktree；与任务路径重叠时停止。
- **范围越界**：停止、保留现场并记录差异，不擅自扩大范围。
- **验证失败**：只在既定产品决定和执行计划内诊断修复；需要改变契约时停止。
- **网络中断**：保留本地 commit；恢复后重新获取 Issue revision 和远端 Git 状态，再继续交付。
- **GitHub 评论失败**：保留本地生成的结构化记录，恢复后补发，不能因此重复 commit 或 push。
- **远端 main 漂移**：只允许一次无冲突 rebase 和完整复验，之后仍漂移则停止。
- **冲突**：不猜测业务取舍，不覆盖他人修改，标记 blocked。
- **高风险发现**：部署、生产数据、迁移、凭据和不可逆操作一律停止并交回 MacBook。

## 旧 Worker 退役与迁移

迁移必须作为一个完整切换，不允许旧、新流程同时处理任务：

1. 再次确认旧数据库没有活跃任务。
2. 为当前旧 Worker 最终 commit 建立明确的 legacy tag。
3. 备份 SQLite、配置和必要日志，以便历史追溯。
4. 停止并卸载 Mac mini LaunchDaemon，验证系统中没有 `codex-worker` 进程。
5. 主分支删除 daemon、SQLite、`codex exec` runner、GitHub App、readiness、Ruleset、PR、merge、恢复状态机及相关安装脚本和测试。
6. README、文档、测试和安装入口一次性切换为 skill-first 架构。
7. 在 MacBook 和 Mac mini 安装同一 commit 的新 skill，并重启或刷新 Codex App 使其加载。
8. 完成端到端演练后，清理旧 venv、worktree 和缓存。
9. GitHub App 私钥先移出运行路径并保持隔离；确认无其他用途后，再单独撤销 App 安装和安全删除私钥。

## EaseWise 项目迁移

EaseWise 与技能仓库同步完成以下变更：

- 删除 `.codex-worker/project.toml`。
- 删除旧 Worker watchdog workflow。
- 替换旧 `codex-task` Issue 模板。
- 增加 `.duomac/project.toml`。
- 创建新的 `duomac:*` 标签。
- 关闭仍为 open 的旧 cancelled Issue。
- 历史 Issue 和旧标签保留审计价值，但不再驱动任何程序。
- 不新增强制 PR、精确身份校验或 Ruleset 依赖。

`codex-mac-worker` 仓库现有“禁止删除默认分支、禁止 non-fast-forward”的极简 Ruleset 可以保留，但技能不得把它作为 ready 或交付前置条件。EaseWise 私有仓库无法使用 Ruleset，不影响新流程。

## 验收与测试

### 自动测试

- Issue 正文结构的成功和失败解析。
- revision 递增、遗漏字段和非法 delivery mode。
- 状态转换与互斥标签。
- task-start、checkpoint 和 delivery 的稳定渲染。
- `.duomac/project.toml` 配置解析与验证 profile 选择。
- context commit、allowed paths、protected paths、文件数和 diff 行数检查。
- 临时 Git remote 中的 `direct-main` 正常推送。
- 远端无冲突前进后的单次 rebase、复验与推送。
- 远端冲突、二次漂移和 force push 拒绝。
- `task-branch` 进入 delivered，而不是误报 completed。
- 网络或 GitHub 写入失败后不重复 commit 和 push。

### 双端演练

1. MacBook 生成一个真实但低风险的小任务，展示完整规格并由用户确认发布。
2. Mac mini Codex App 可见地获取并领取 Issue。
3. Mac mini 执行至少两个里程碑并写检查点，无需逐次批准。
4. 通过项目验证后按指定 delivery mode 交付。
5. 从 Issue 追溯 revision、上下文、计划、检查点、commit、diff、验证和验收结果。
6. 重启 Mac mini，确认旧 LaunchDaemon 不会恢复，Codex App skill 仍可在新对话中使用。

## 成功标准

- 两台 Mac 只通过可见的 Codex App 执行开发任务。
- GitHub Issue 是跨设备任务沟通和审计入口，正文是唯一当前任务契约。
- 任一执行阶段都能明确知道当前 revision、已完成内容、下一步和阻塞项。
- 长任务可以连续执行，不因检查点等待 MacBook 批准。
- 是否派发始终由用户与 MacBook Codex 决定。
- 默认 direct-main 提高效率，同时对远端漂移、冲突和 force push 保持保护。
- 新流程完全不依赖 GitHub App、PR、Ruleset、后台 Worker 或 Goal 模式。
- 旧 Worker 不会因残留服务、配置或标签再次进入半工作状态。
