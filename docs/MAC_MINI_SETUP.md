# Mac mini 安装与上线

这些步骤需要你在 Mac mini 上亲自完成。先只连接一个测试仓库；不要一开始给 EaseWise 或其他生产相关仓库全面授权。

## 1. 系统与物理条件

记录设备信息：

```bash
system_profiler SPHardwareDataType
sw_vers
uname -m
fdesetup status
```

完成仍受支持版本的 macOS 安全更新。Worker 需要在无人登录时启动，因此本方案锁定为 FileVault 关闭；这会降低磁盘被盗后的保护，Mac mini 必须放在物理安全的位置。

禁止主机和磁盘睡眠，并启用来电恢复：

```bash
sudo pmset -a sleep 0
sudo pmset -a disksleep 0
sudo pmset -a autorestart 1
pmset -g custom
```

若机型和系统提供“连接电源时启动”，在“系统设置 → 能源”中设为“始终”。配置向日葵开机启动和无人值守访问，但只把它作为人工维护通道。完成一次退出登录和一次重启后的远程接入测试。

## 2. 安装工具并登录

安装 Xcode Command Line Tools、Git、Python 3.12、Node.js（EaseWise 前端验证需要 npm）和 GitHub CLI。默认权限档案只读开放 Apple Silicon Homebrew 的 `/opt/homebrew` 和 Python.org Framework；不要把工具安装在含有凭据的自定义目录。使用 Homebrew 时可以执行：

```bash
xcode-select --install
brew install python@3.12 node git gh
gh auth login
```

安装器会用受限权限档案执行一次 Worker 虚拟环境 Python 冒烟测试。若出现 `Operation not permitted`，不要扩大到整个用户目录；改用 `/opt/homebrew` 下的 Apple Silicon Homebrew 或 Python.org 签名安装包后重建虚拟环境。

安装 ChatGPT 桌面应用。Worker 不复用日常 Codex 配置，而是在安装目录中使用独立 `CODEX_HOME`。先运行一次安装器，让它创建受控权限档案和配置示例：

```bash
./scripts/install_macos.sh
CODEX_HOME="$HOME/Library/Application Support/CodexWorker/codex-home" \
  /Applications/ChatGPT.app/Contents/Resources/codex login
CODEX_HOME="$HOME/Library/Application Support/CodexWorker/codex-home" \
  /Applications/ChatGPT.app/Contents/Resources/codex login status
```

安装器每次都会恢复受审查的 Worker 权限档案：用户目录默认不可读、当前 worktree 可写、网络关闭，并明确关闭 Goal、apps 和多代理。不要把个人 `~/.codex/config.toml` 复制进去。

在一个无敏感信息的测试仓库运行只读冒烟测试，确认不弹出人工批准且能自行退出：

```bash
printf '%s\n' 'Inspect this repository and return a short summary. Do not modify files.' | \
  env CODEX_HOME="$HOME/Library/Application Support/CodexWorker/codex-home" \
  /Applications/ChatGPT.app/Contents/Resources/codex exec \
  --strict-config --json --cd /path/to/test-repository -
```

再做一次拒绝读取验证：让测试仓库中的 Codex 尝试读取该仓库之外的任意无敏感文件，预期返回 `Operation not permitted`。验证时不要用真实私钥作为探针。

## 3. 创建 GitHub App

在 GitHub 网页创建独立的 GitHub App，例如 `easewise-mac-worker`。不需要 webhook。Repository permissions 设置为：

- Contents: Read and write
- Issues: Read and write
- Pull requests: Read and write
- Checks: Read-only
- Actions: Read-only
- Administration、Deployments、Environments、Workflows: None

仅把 App 安装到第一个测试仓库。记录 App ID 和 Installation ID，生成私钥后只在 Mac mini 本地移动它：

```bash
mkdir -p "$HOME/Library/Application Support/CodexWorker/secrets"
chmod 700 "$HOME/Library/Application Support/CodexWorker/secrets"
mv /path/to/downloaded-key.pem \
  "$HOME/Library/Application Support/CodexWorker/secrets/github-app.pem"
chmod 600 "$HOME/Library/Application Support/CodexWorker/secrets/github-app.pem"
```

不要通过聊天、Issue、Git 仓库或网盘传输私钥。

## 4. 配置目标仓库保护

先把目标仓库中的以下文件经人工 PR 合并到默认分支：

- `.codex-worker/project.toml`
- `.github/ISSUE_TEMPLATE/codex-task.yml`
- `.github/workflows/codex-worker-watchdog.yml`

在 GitHub 为 `main`、`master` 和发布分支建立 Ruleset：必须通过 PR、至少一次人工批准、CI 通过、对话已解决、禁止 force push。不要把 Worker App 放入 bypass 名单。随后用 Worker App 身份做一次负向验证，确认直接 push 主线和 merge 都被拒绝。

创建状态标签：

```bash
./scripts/bootstrap_repository.sh OWNER/REPO
```

## 5. 写入 Worker 配置

若第二节尚未执行，先运行一次安装脚本。它会创建目录、专用权限档案、虚拟环境和渲染后的配置示例，然后因缺少正式配置而安全退出：

```bash
./scripts/install_macos.sh
```

将生成的 `worker.toml.example` 复制为 `worker.toml`，填写 GitHub 登录名、App ID、Installation ID、仓库名和 clone URL。不要扩大仓库列表。验证 `codex_path` 与本机实际路径一致。

再次运行安装：

```bash
./scripts/install_macos.sh
./scripts/doctor_macos.sh
```

安装器把程序安装到 `~/Library/Application Support/CodexWorker/`，日志写入 `~/Library/Logs/CodexWorker/`，并注册：

- `/Library/LaunchDaemons/com.easewise.codex-worker.plist`
- `/Library/LaunchDaemons/com.easewise.codex-worker-backup.plist`

LaunchDaemon 由 root 注册，但 Worker 进程使用当前个人账户运行。每日维护任务通过 SQLite backup API 备份状态库，并对超过 10 MiB 的日志做压缩轮转；保留最近 14 个每日数据库备份。

## 6. 上线前演练

按顺序完成并保存结果：

1. 手工 `launchctl kickstart`，确认 60 秒内出现心跳。
2. 创建一个只改测试文档的低风险任务，确认 Issue → worktree → 测试 → Draft PR。
3. 制造测试失败，确认最多一次自动修复后停止。
4. 测试越界路径、超大 diff、Codex 自行 commit 和任务块篡改均进入 `needs-attention`。
5. 测试 `pause`、`resume`、`retry`、`cancel`、`revise` 的幂等性。
6. 杀死 Codex 子进程和 Worker，确认 launchd 恢复且没有重复 commit、评论或 PR。
7. 断网后恢复，确认 outbox 重放但不重复外部写入。
8. 退出登录，确认 Worker 仍运行；整机重启，确认无需登录便出现心跳。
9. 关闭 Mac mini 或 Worker 且保留 queued/active Issue，确认 Watchdog 在 10～15 分钟内告警。
10. 人工合并 Draft PR，确认 Worker 之后才关闭 Issue 并标记 `codex:completed`。

通过全部演练后，才为 EaseWise 安装 App。先连续执行至少十个低风险任务并观察两周；之后每次只增加一个仓库。
