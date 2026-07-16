# 安全边界

- GitHub App 只安装到明确授权仓库；除非用户有意选择全部个人仓库，否则 installation 保持 repository-scoped。安装令牌按需签发并只存在于 Worker 内存或临时 Git 凭据环境。
- App 私钥、安装令牌和部署凭据不进入 Codex 子进程、Git remote URL、prompt、Issue 或日志。
- `codex exec` 使用独立 `CODEX_HOME` 和 `--strict-config`；权限档案默认拒绝读取用户目录，只允许最小系统文件和当前工作区，且关闭网络、apps、多代理与 Goal/“目标”模式。
- 权限档案只额外开放 `/opt/homebrew` 与 Python.org 的 `/Library/Frameworks/Python.framework` 为只读工具链根；GitHub 私钥所在的 `~/Library/Application Support/CodexWorker/secrets` 仍不可读。
- Worker 不传旧式 `--sandbox` 或 `sandbox_mode` 覆盖权限档案；目标仓库若包含 `.codex/config.toml`，任务会被拒绝，防止项目配置扩大权限。
- Issue 只能选择仓库内 `.codex-worker/project.toml` 已审查的测试命令。
- `.codex-worker/project.toml` schema v2 必须固定可信的数字 `worker_github_app_id`；仓库发现、执行前校验、就绪证明和交付审查都拒绝其他 GitHub App。v1 任务安全停止，升级后重新派单。
- Worker 在 commit 前检查 Git HEAD、允许路径、敏感路径、文件数、diff 行数、密钥特征和大型二进制文件。
- Worker 只 push `codex/*` 任务分支并创建 Draft PR；Ruleset 禁止直接 push 和 force push，并定义手动或单所有者自动合并的最终边界。Worker App 不在 Ruleset bypass 名单。
- Codex 子进程不负责 commit、push、PR、部署或 merge。Worker 只在可信本地 `merge_mode = "automatic"` 与仓库 automatic Ruleset 同时成立时，通过独立的持久化流程合并自己创建并验证过的精确 PR head。
- MacBook 的个人 `gh` token 不进入 Mac mini。手动模式下，`codexctl task merge` 只接受当前 PR 的 explicit approval、完整 `--expected-head` 和 `--expected-fingerprint`；接入 bootstrap 是唯一允许自审的例外。
- Goal/“目标”模式、生产部署、生产数据、高风险任务、自动回滚、仓库级 future PR 批准都被排除。自动合并不会授权 Worker 创建任务、扩大范围或合并其他主体的 PR。
- FileVault 关闭是冷启动无人值守的已锁定取舍，因此必须依赖物理安全、最小仓库授权和本地私钥权限降低风险。
- GitHub Watchdog 只评论告警，不领取、恢复或重复执行任务。

发现私钥泄露时，立即在 GitHub 撤销私钥或删除 App installation，停止 LaunchDaemon，轮换凭据并审计 Issue、分支、PR 与日志。不要通过新的聊天消息发送替代私钥。
