# 日常操作

## MacBook 派单

先确认仓库 ready，再确保设计与上下文已经 commit 并 push。任务规格必须只有一个目标、可验证 acceptance、已提交的 context files、最小 allowed paths 和仓库已有的验证 profile。

```bash
codexctl repo status OWNER/REPO
codexctl task create --repo OWNER/REPO --title '单一结果' --spec task.yaml
codexctl task status https://github.com/OWNER/REPO/issues/123
```

`create` 默认先显示完整规格并再次询问。不要用 Goal/“目标”模式代替任务拆分。

## 控制命令

```bash
codexctl task pause ISSUE_URL
codexctl task resume ISSUE_URL
codexctl task retry ISSUE_URL
codexctl task cancel ISSUE_URL
codexctl task revise ISSUE_URL --requirements revision.yaml
```

- `pause`：停止进程组，保留 worktree 和会话标识。
- `resume`：只用于显式恢复暂停或崩溃现场，最多一次。
- `retry`：只用于 Worker 已持久化标记为可重试的网络、GitHub 5xx、限流或可恢复 push 等基础设施故障；没有内部可重试分类时命令会被拒绝。
- `revise`：在原分支和原 Draft PR 上启动全新会话，不续用旧会话扩大范围。
- `cancel`：停止任务并保留现场，默认七天后再人工清理。

高风险、权限拒绝、认证失败、磁盘不足、冲突、任务非法、范围越界和测试持续失败都需要人工处理，不应 retry。

### 交付阶段重试

当 Codex 已完成、仓库验证已通过、Worker 已创建本地提交，但 push、Draft PR 或最终状态写入发生瞬态故障时，Worker 会先持久化一个 `delivery checkpoint`。这类 `retry` 只复用 checkpoint 中的同一个提交并重新运行仓库批准的验证命令；它 **does not rerun Codex**，不会恢复旧会话，也不会创建替代提交。

checkpoint 在第一次网络写入前以单个 SQLite 事务进入可恢复状态，证据之后不可改写。若重新验证期间收到 `pause`，后续 `resume` 仍走交付重试路径，不会恢复 Codex 会话；若已创建 Draft PR 但命令确认尚未落盘，重启后会用稳定任务状态对账并确认原命令，不会再次执行。

每次交付重试都有独立的 **30-minute** 硬上限。Worker 会在联网前重新核对任务哈希、授权、branch、worktree、HEAD、唯一父提交、项目配置哈希、验证命令、范围、secret 和二进制限制。认证、权限、配置漂移、范围越界或验证失败会永久取消重试资格；只有经过分类的瞬态 Git/GitHub 交付错误可以再次请求重试。

每次操作必须由新的 **new command ID** 和新的明确批准触发。已执行命令即使 Worker 重启也不会重放；进程在命令执行中崩溃时，未完成的命令记录会在恢复后继续，并依靠相同提交和 Draft PR head 分支对账避免重复外部写入。不要直接编辑 SQLite、delivery checkpoint 或 outbox。

配置 `git_proxy_url` 后，Git 网络命令保持三次总尝试上限，路由顺序固定为 `proxy → direct → proxy`。瞬态连接错误才会切换路由；认证、权限、证书、仓库不存在和本地 ref 冲突会立即停止。若本地代理未启动，第二次尝试仍可通过直连完成冷启动任务。

## 服务与日志

```bash
sudo launchctl print system/com.easewise.codex-worker
sudo launchctl kickstart -k system/com.easewise.codex-worker
tail -f "$HOME/Library/Logs/CodexWorker/worker.stderr.log"
./scripts/doctor_macos.sh
```

升级前先确认没有 running/verifying/retrying 任务。Worker 不自我更新；从受审查的 commit 更新本仓库，再重新运行 `install_macos.sh`。安装器替换 Python 环境和 plist，但保留 SQLite、worktrees、备份、日志和私钥。

## 故障处置

1. 先看 Issue 状态评论、任务哈希、progress_at 和 Watchdog 评论。
2. 在 Mac mini 运行 doctor，检查磁盘、Codex 登录、私钥权限和两个 LaunchDaemon。
3. 检查日志和 SQLite 备份，不手工删除 outbox 或 worktree。
4. 若 Issue 领取后被编辑，保留现场，重新创建新的 Issue；不要改写已冻结任务。
5. 若远程分支、PR 与 SQLite 不一致，先人工对账再决定 cancel 或重新派单。

对于升级前已经生成本地交付提交、但还没有 delivery checkpoint 的旧任务，先通过 Worker/EventStore 只读接口核对：任务哈希未变、worktree 干净且位于记录分支、HEAD 只有 context commit 一个父提交、存在同 session 的唯一成功 run、结构化结果完整，并且当前验证再次通过。证据全部成立后 Worker 才会落 checkpoint；任一条件不成立就永久拒绝，不猜测也不改库。

以保留的 Issue #12 为例，升级后的顺序是：

1. 核对已安装的精确 merge commit、LaunchDaemon PID 和日志健康。
2. 通过只读接口展示 task/run/worktree/HEAD/parent 的可恢复证据。
3. 获得新的明确 retry 批准，再发布一个全新的 command ID。
4. 确认 Draft PR 的 delivery commit 与恢复前本地 HEAD 完全一致。

旧命令 `503e56c5-64a7-474b-8364-299c6f929272` 已执行，禁止复用。

Worker 永不调用 merge API。MacBook 的受控命令确认合并后，Worker 才把 Issue 标记为 completed。

## 仓库接入

```bash
codexctl repo status OWNER/REPO
codexctl repo onboard --repo OWNER/REPO --project-config project.toml
codexctl repo finalize OWNER/REPO#PR --expected-head FULL_HEAD_SHA
```

`repo onboard` 后先展示不可变 PR 快照并停止。只有 explicit approval 明确指向该 PR，才能 finalize。`awaiting-worker` 是正常中间态；等待 Mac mini readiness attestation，变为 `ready` 才派单。

## 审查与辅助合并

```bash
codexctl task review ISSUE_URL
codexctl task merge ISSUE_URL --expected-head FULL_HEAD_SHA --expected-fingerprint APPROVAL_FINGERPRINT
```

`task review` 永远只读。展示 Checks、测试证据、acceptance、风险、review threads、审批指纹和 head SHA 后停止。明确批准当前 PR/head/fingerprint 才执行 merge；任一值、Checks 或 Ruleset 改变就必须重新 review 和批准。不存在 automatic merge、仓库级永久批准或 future PR 授权。

MacBook 使用个人 `gh` token 做 review/approval/squash merge；Mac mini 不持有该 token。
