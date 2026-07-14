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
