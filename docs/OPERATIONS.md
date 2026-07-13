# 日常操作

## MacBook 派单

先确保设计与上下文已经 commit 并 push。任务规格必须只有一个目标、可验证 acceptance、已提交的 context files、最小 allowed paths 和仓库已有的验证 profile。

```bash
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

Worker 永不调用 merge API。你审查并人工合并 Draft PR 后，Worker 才把 Issue 标记为 completed。
