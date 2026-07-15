# GitHub API 代理隔离设计

## 背景

Mac mini 上的 Worker 由 LaunchDaemon 在用户登录前启动。macOS 系统代理当前指向 Clash Verge 的 `127.0.0.1:7897`，而 Clash Verge 只有在用户登录后才运行。Python 的代理发现机制会把该系统代理交给 httpx，导致 Worker 在冷启动、Clash 重启或 Clash 退出时无法访问 GitHub API。

现场日志确认失败发生在 httpx 的 HTTP 代理传输层，错误包括 `Connection refused` 和 TLS `UNEXPECTED_EOF_WHILE_READING`。同一台 Mac mini 已验证不经过系统代理可直接访问 `https://api.github.com`。

## 目标

- Worker 的 GitHub App 令牌签发请求和所有 GitHub REST/GraphQL API 请求不继承系统或环境代理。
- Codex CLI、Git 以及 Worker 的其他子进程保持现有网络行为。
- Mac mini 在尚未登录、Clash 未启动或 Clash 退出时仍能轮询和更新 GitHub 任务。
- 保留现有的 GitHub 错误分类、重试和 durable outbox 行为。

## 非目标

- 不管理、启动、停止或配置 Clash Verge。
- 不为 Codex CLI 或 Git 增加统一代理策略。
- 不改变 Issue #5 的任务内容、任务状态机或自动重试规则。
- 不清理或直接修改现有 SQLite/outbox 记录。

## 方案

在 `GitHubAppAuth` 和 `GitHubClient` 创建各自的 `httpx.Client` 时显式设置 `trust_env=False`。

这一设置仅作用于 Worker 进程内部的 GitHub HTTP 客户端：

1. `GitHubAppAuth` 直接连接 GitHub API，签发短期 installation token。
2. `GitHubClient` 直接连接 GitHub REST 和 GraphQL API。
3. Worker 启动的 Codex CLI 与 Git 进程不受该参数影响。

不新增用户可配置开关。当前部署已验证 GitHub 直连可用，而无人值守 Worker 不应隐式依赖登录后才存在的桌面代理；加入开关会增加不可验证的部署分支和冷启动风险。

## 错误处理

连接失败仍由现有逻辑包装为 `GitHubError(status_code=None, retryable=True)`。HTTP 429 和 5xx 的分类保持不变。该修改只改变连接路径，不改变重试次数、任务状态或 outbox 交付语义。

Issue #5 已因基础设施故障停在本地 `needs-attention`，代码升级本身不会绕过控制命令规则。升级并验证 Worker 稳定访问 GitHub 后，再通过受控操作恢复或重新发布该任务。

## 测试与验收

采用测试驱动实现：

1. 先增加回归测试，捕获传给两个 `httpx.Client` 的初始化参数，并断言二者都明确使用 `trust_env=False`；测试在生产代码修改前必须失败。
2. 加入最小生产代码使回归测试通过。
3. 运行完整 pytest 测试集。
4. 在 Mac mini 更新 Worker 后，验证：
   - Worker 服务保持运行；
   - GitHub App installation token 可以签发；
   - GitHub API 读取成功；
   - 在不依赖 Clash 监听端口的条件下，GitHub API 仍可访问；
   - 任务状态和 outbox 未被人工篡改。

## 发布与回退

变更通过独立分支和 PR 发布。合并后，Mac mini 从远端主分支更新虚拟环境中的 Worker 包并重启 LaunchDaemon。

若升级验证失败，恢复到升级前的已安装提交并重启服务；保留 SQLite、日志、worktree 和 outbox 现场。回退不恢复 Issue #5 的执行，任务控制仍需单独确认。
