# 仓库接入与人工授权合并设计

日期：2026-07-14
状态：已批准设计，待实施计划

## 1. 背景与目标

Codex 双 Mac 工作流将职责分成两个身份：

- MacBook 是产品经理与派单端，负责 PRD、Project Card、Spec、任务拆分、审查和最终决策。
- Mac mini 是执行端，负责领取边界明确的 GitHub Issue、运行 `codex exec`、验证修改并创建 Draft PR。

当前 Worker 已坚持“不自动合并”，但新仓库第一次接入以及后续 PR 合并仍依赖用户进入 GitHub 网页操作，容易形成流程卡点。

本设计的目标是：

1. 用一个可重复、可审计的命令把个人账号下的任意授权仓库接入 Worker。
2. 用户继续保留每个 PR 的最终合并决定，但不必亲自操作 GitHub 网页。
3. 用户在当前对话中明确批准某个具体 PR 后，由 MacBook 使用用户自己的 GitHub 身份完成机械性的复核与合并。
4. Worker GitHub App 继续不能更新默认分支或执行合并，也不进入 Ruleset 绕过名单。
5. 全流程不使用 Codex Goal/“目标”模式。

## 2. 非目标

首版明确不支持：

- Worker 自主判断并合并 PR。
- 以一次授权覆盖后续多个 PR、整个仓库或某个时间窗口。
- 自动合并高风险任务、生产部署、数据库迁移或不可逆操作。
- 自动降低 Ruleset、绕过 CI、忽略未解决的审查意见。
- 多 Worker 并发领取同一仓库任务。
- 把个人 GitHub 凭据复制到 Mac mini。

## 3. 身份与信任边界

### 3.1 Mac mini Worker 身份

Mac mini 只使用专用 GitHub App 安装令牌。App 负责：

- 读取结构化任务 Issue；
- 更新任务状态与审计评论；
- 推送 `codex/*` 任务分支；
- 创建和更新 Draft PR。

GitHub 的 Contents/Pull requests 写权限需要支持任务分支和 Draft PR，不能单独剥离其中的 merge API。因而真正的强制边界由两层组成：Worker 程序不实现 merge 调用；默认分支 Ruleset 只允许用户身份完成更新，并排除 Worker App 的绕过能力。接入验收必须用 App 身份实测更新默认分支和 merge 均被拒绝。

### 3.2 MacBook 用户身份

MacBook 的 `codexctl` 通过本机 `gh` 登录态使用用户个人身份，负责：

- 创建任务 Issue；
- 初始化仓库接入；
- 读取 PR、Checks、审查线程与 Ruleset 状态；
- 在用户针对具体 PR 明确批准后执行 review/merge；
- 完成接入后的标签和 Ruleset 配置。

个人凭据只保留在 MacBook，不交给 Worker 子进程或 Mac mini。

### 3.3 决策与执行分离

“用户决定合并”和“MacBook 执行合并”是两个独立步骤：

1. MacBook 先生成不可变的审查摘要，绑定 PR、base、head SHA、任务哈希和检查结果。
2. 用户在当前对话中明确批准该摘要对应的 PR。
3. MacBook 在执行前重新读取远端状态，并确认 head SHA 与批准时一致。
4. 只有所有门禁仍通过时，MacBook 才使用用户身份合并。

CLI 本身不能推断用户是否在对话中批准；对话层的 `dispatch-codex-task` skill 负责等待确认，CLI 负责验证远端状态和不可变标识。

## 4. 仓库接入设计

### 4.1 命令界面

新增以下命令：

```text
codexctl repo onboard --repo OWNER/REPO
codexctl repo onboard --repo OWNER/REPO --adopt-pr NUMBER
codexctl repo status OWNER/REPO
codexctl repo finalize PR_URL --expected-head SHA
```

`repo onboard` 是准备阶段，不直接合并；`repo finalize` 只能在用户审阅并明确批准后调用。

### 4.2 新仓库标准文件

接入 PR 只允许增加或修改以下三个文件：

```text
.codex-worker/project.toml
.github/ISSUE_TEMPLATE/codex-task.yml
.github/workflows/codex-worker-watchdog.yml
```

`project.toml` 必须明确默认分支、允许风险、受保护路径、diff 上限、时间上限以及来自仓库的验证命令。Issue 模板只负责人工备用派单。Watchdog 只告警，不执行任务。

### 4.3 `repo onboard` 流程

命令按顺序执行：

1. 验证目标仓库存在、用户身份有管理权限、GitHub App 已安装到该仓库。
2. 读取默认分支、Ruleset、标签、现有接入文件和未关闭的接入 PR。
3. 生成或校验三个标准文件；验证命令必须由用户或仓库现有文档提供，不允许模型臆测后静默采用。
4. 创建 `codex/onboard-worker` 分支和 Draft PR，或采用显式指定的现有 PR。
5. 拒绝接入 PR 中任何不属于三个标准文件的修改。
6. 输出审查清单：PR URL、base SHA、head SHA、文件列表、diff、App 权限状态、Ruleset 差距和待创建标签。
7. 停止并等待用户在当前对话中批准。

命令应可重复运行。若远端已经处于目标状态，则报告 `ready`，不重复创建分支、PR、标签或评论。

### 4.4 采用现有接入 PR

对于 EaseWise 当前的 PR #1，使用：

```text
codexctl repo onboard --repo qiaozhang1225/EaseWise --adopt-pr 1
```

采用前必须验证 PR 仅修改三个标准文件、来源分支属于用户可控仓库、base 是当前默认分支，并记录准确的 head SHA。任何额外修改都会使采用失败，要求先拆分 PR。

### 4.5 `repo finalize` 流程

收到用户针对该 PR 的明确批准后，MacBook 调用：

```text
codexctl repo finalize PR_URL --expected-head SHA
```

执行顺序为：

1. 重新读取 PR；若 head SHA 改变，立即失效并要求重新审查。
2. 再次验证精确文件范围、Checks、合并状态和未解决审查线程。
3. 使用用户的 GitHub 身份合并接入 PR。
4. 创建全部互斥 `codex:*` 状态标签。
5. 创建或校验默认分支 Ruleset：必须 PR、禁止 force push、禁止删除、默认分支只允许用户身份更新，并确保 Worker App 不可绕过。
6. 检查三个接入文件已经位于默认分支。
7. 验证 Worker App 能访问仓库，但不能直接推送或合并默认分支。
8. 输出仓库 readiness 报告。

接入 PR 可能由用户本人创建，GitHub 不允许作者批准自己的 PR。因此首次接入允许在对话批准、精确文件校验和 head SHA 绑定后直接由用户身份合并；Ruleset 在此后立即建立。这个例外只适用于仓库接入 PR，不适用于普通 Worker 任务 PR。

### 4.6 标签与 readiness

接入完成后必须存在：

```text
codex:queued
codex:claimed
codex:running
codex:verifying
codex:retrying
codex:awaiting-review
codex:needs-attention
codex:completed
codex:cancelled
```

`codexctl repo status` 只有在以下条件全部成立时才返回 `ready`：

- GitHub App 已授权该仓库；
- 三个标准文件位于默认分支且 schema 受支持；
- 所有状态标签存在且定义一致；
- Ruleset 已启用且 Worker App 不可绕过；
- 配置中的验证命令非空且可解析；
- 没有未完成或相互冲突的接入 PR。

## 5. 日常审查与辅助合并设计

### 5.1 命令界面

新增：

```text
codexctl task review ISSUE_URL
codexctl task merge ISSUE_URL --expected-head SHA --expected-fingerprint FINGERPRINT
```

`task review` 只读；`task merge` 只在对话层收到当前 PR 的明确批准后执行。

### 5.2 审查摘要

`task review` 必须汇总并展示：

- Issue URL、冻结任务块哈希和 context commit；
- PR URL、base 分支、base SHA 和 head SHA；
- 修改文件、diff 规模和是否触及保护路径；
- Worker 执行记录、实际 Codex 模型与 CLI 版本；
- 仓库批准的测试命令及结果；
- 验收条件逐项状态；
- 未解决的 review threads、冲突、风险和人工依赖。

输出同时包含一个审批指纹，由仓库、Issue、PR、任务哈希和 head SHA 计算。指纹用于帮助人确认审批对象，不替代 GitHub 的远端校验。

### 5.3 用户批准语义

只有类似“批准合并 EaseWise PR #12”或“批准合并刚才摘要中的 PR”这样的明确表述才构成授权。以下内容不构成授权：

- “看起来可以”；
- 对设计方案的批准；
- 对整个仓库、某类任务或未来 PR 的概括授权；
- 旧对话中的授权；
- head SHA 已改变的 PR 的历史授权。

授权是一次性的，只对应一个 PR 的一个 head SHA。合并失败或远端状态改变后必须重新审查；不得自动扩大或续期授权。

### 5.4 合并门禁

`task merge` 必须全部满足：

1. PR 来源分支为 `codex/*`，且由已授权 Worker 身份创建或更新。
2. PR 与唯一的 Worker Issue、任务哈希和 context commit 一致。
3. 当前 head SHA 等于 `--expected-head`，审批指纹等于 `--expected-fingerprint`，两者都等于用户看到的审查摘要。
4. PR 已不再是 Draft，或命令在复核成功后先将其标记为 Ready for review。
5. GitHub Checks 全部通过，required checks 没有 pending、skipped 异常或 failure。
6. Worker 记录的仓库验证命令全部成功。
7. diff 未超出 `allowed_paths`、文件数、行数或受保护路径约束。
8. PR 可干净合并，没有冲突，也没有未解决 review threads。
9. 风险不为 high，不包含生产部署、生产数据、凭据、迁移或不可逆操作。
10. 默认分支 Ruleset 仍有效，Worker App 仍不在绕过名单。

任一门禁失败都必须停止并给出具体原因，不得自动降低条件。

### 5.5 合并动作与审计

通过门禁后，MacBook 使用用户身份：

1. 在可用且非自审的情况下提交 GitHub approval review；
2. 按仓库策略执行 squash merge；
3. 在 PR 留下结构化审计评论，记录审批指纹、批准者、任务哈希、合并前 head SHA 和时间；
4. 等待 Worker 对账并将 Issue 标记为 `codex:completed`。

审计评论不复制聊天全文，也不包含凭据。若 GitHub merge API 返回状态不确定，命令先查询 PR 和默认分支，不得盲目重试造成重复动作。

## 6. Skill 交互设计

扩展 `dispatch-codex-task` skill，使其覆盖仓库生命周期：

- 当用户希望接入仓库时，先调用 `repo status`，再准备 onboarding PR。
- 在创建或采用接入 PR 后，展示完整摘要并暂停，等待明确批准。
- 当用户要求审查任务时，调用 `task review` 并把门禁结果翻译成简洁说明。
- 只有在用户明确指定或明确指代当前摘要中的 PR 后，才调用 `repo finalize` 或 `task merge`。
- 如果 SHA 或检查状态变化，skill 必须重新展示摘要并再次请求批准。
- skill 不使用 Goal 模式，不代替用户决定产品范围，也不自动合并未来 PR。

## 7. 状态机与失败处理

仓库接入状态：

```text
unconfigured -> onboarding-pr -> awaiting-approval -> finalizing -> ready
                       |                  |              |
                       +-> blocked <------+--------------+
```

普通任务交付状态仍由 Worker 管理；辅助合并只发生在 `codex:awaiting-review`：

```text
codex:awaiting-review -> reviewed -> explicitly-approved -> merged
          |                 |               |
          +-> needs-attention <-------------+
```

关键失败策略：

- SHA 改变：审批失效，重新 review。
- Checks 失败或 pending：停止，不重跑业务任务；按现有 retry/revise 规则处理。
- 合并冲突：停止，要求 revise 或人工决定。
- 权限不足：报告缺失权限，不建议提高 Worker App 权限。
- GitHub 429/5xx：有界重试；状态不确定时先对账。
- Ruleset 漂移：仓库降级为 `blocked`，不得派发或合并新任务。
- 本地 `gh` 登录失效：停止并要求用户在 MacBook 重新登录。

## 8. 安全与幂等要求

- 所有写操作必须包含稳定的 operation ID，并在本地记录结果。
- 创建分支、PR、标签、评论和 merge 前都先查询远端，支持崩溃后安全重入。
- `--expected-head` 必填，不提供“使用最新 SHA”的隐式选项。
- 命令日志对 token、Authorization header、私钥路径内容做脱敏。
- MacBook 个人 token 不传给 `codex exec`；执行 shell 的环境应清除无关凭据。
- 结构化审计记录不得被 Issue 文本中的伪造字段替代；身份以 GitHub API 返回值为准。
- 接入和合并命令都默认 dry inspection，任何不可逆动作必须位于显式 finalize/merge 子命令。

## 9. 验收标准

实现完成前必须通过：

1. 空白测试仓库可从 `unconfigured` 接入到 `ready`，重复运行不产生重复资源。
2. 可安全采用一个只含三个标准文件的现有 onboarding PR。
3. onboarding PR 含第四个文件时被拒绝。
4. 用户批准后、执行前 PR head SHA 改变时合并被拒绝。
5. 普通 Worker PR 只有在当前对话明确批准且所有门禁通过时才合并。
6. Checks 失败、越界路径、超限 diff、冲突、未解决线程和 high risk 均阻断合并。
7. Worker App 直接 push 默认分支和调用 merge API 均失败。
8. MacBook 个人凭据不会出现在 Mac mini、Worker 日志、Issue、PR 或 Codex 子进程环境中。
9. GitHub 5xx 或本地崩溃后重新运行不会重复创建 PR、评论或合并。
10. EaseWise 完成接入后可创建一个低风险连通性任务，并由 Mac mini 交付 Draft PR。

## 10. EaseWise 首次落地顺序

实施完成后，EaseWise 按以下顺序接入：

1. 运行 `repo onboard --adopt-pr 1` 并审查 PR #1 的准确 head SHA 与三个文件。
2. 用户在当前对话中明确批准 PR #1。
3. 运行 `repo finalize`，合并接入 PR、创建标签、配置 Ruleset 并验证 `ready`。
4. 发布一个最小、只读或文档级的连通性任务，验证 Issue 到 Draft PR 的完整链路。
5. 再发布“我的评测记录”中四柱八字卡片布局修复任务。
6. MacBook 展示 Worker PR 的 diff、测试和门禁摘要；用户明确批准后，由 MacBook 代执行 squash merge。

整个过程的最终产品决策始终由用户做出；MacBook 只负责把已批准决策可靠地落实到 GitHub，Mac mini 只负责有界执行。
