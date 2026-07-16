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

安装器会分别用执行权限档案和 preparation 权限档案执行 Python 冒烟测试。若出现 `Operation not permitted`，不要扩大到整个用户目录；改用 `/opt/homebrew` 下的 Apple Silicon Homebrew 或 Python.org 签名安装包后重建虚拟环境。

安装 ChatGPT 桌面应用。Worker 不复用日常 Codex 配置，而是在安装目录中使用独立 `CODEX_HOME`。先运行一次安装器，让它创建受控权限档案和配置示例：

```bash
./scripts/install_macos.sh
CODEX_HOME="$HOME/Library/Application Support/CodexWorker/codex-home" \
  /Applications/ChatGPT.app/Contents/Resources/codex login
CODEX_HOME="$HOME/Library/Application Support/CodexWorker/codex-home" \
  /Applications/ChatGPT.app/Contents/Resources/codex login status
```

安装器每次都会恢复受审查的 Worker 权限档案：用户目录默认不可读、当前 worktree 可写，并明确关闭 Goal、apps 和多代理。`codex-worker` 执行与验证档案保持网络关闭；只有仓库配置中受审查的 preparation 命令使用 `codex-worker-preparation`，且只能访问 `pypi.org`、`files.pythonhosted.org` 和 `registry.npmjs.org`。不要把个人 `~/.codex/config.toml` 复制进去，也不要给 Codex 执行档案开放网络。

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

App installation 默认只选择明确授权的仓库。只有你有意让全部个人仓库都采用这套治理时才选择“All repositories”；安装范围不是 Worker 可自行扩大。先只连接第一个测试仓库。记录 App ID 和 Installation ID，生成私钥后只在 Mac mini 本地移动它：

```bash
mkdir -p "$HOME/Library/Application Support/CodexWorker/secrets"
chmod 700 "$HOME/Library/Application Support/CodexWorker/secrets"
mv /path/to/downloaded-key.pem \
  "$HOME/Library/Application Support/CodexWorker/secrets/github-app.pem"
chmod 600 "$HOME/Library/Application Support/CodexWorker/secrets/github-app.pem"
```

不要通过聊天、Issue、Git 仓库或网盘传输私钥。

## 4. 配置目标仓库保护

在 MacBook 运行 `codexctl repo status OWNER/REPO`，再用 `codexctl repo onboard` 准备只含以下三项的接入 PR：

- `.codex-worker/project.toml`
- `.github/ISSUE_TEMPLATE/codex-task.yml`
- `.github/workflows/codex-worker-watchdog.yml`

显示完整快照后停止。明确批准该接入 PR，才在 MacBook 执行：

```bash
codexctl repo finalize OWNER/REPO#PR --expected-head FULL_HEAD_SHA
```

`.codex-worker/project.toml` 必须使用 `schema_version = 2`，包含 `worker_github_app_id = <数字 App ID>`，并与 Mac mini `worker.toml` 的 `github_app_id` 完全一致。旧 v1 配置和保留 worktree 会安全停止；合并 v2 配置后重新派单，不恢复旧 v1 会话。

该命令负责接入 bootstrap 的唯一自审例外、标准标签和默认分支 Ruleset。Worker App 不在 bypass 名单；用 Worker 身份负向验证直接 push 主线和 merge 均失败。随后状态进入 `awaiting-worker`，Mac mini 只处理探针且不运行 Codex；只有由配置中指定 App 写出的匹配 Bot attestation 出现后状态才是 `ready`。

## 5. 写入 Worker 配置

若第二节尚未执行，先运行一次安装脚本。它会创建目录、专用权限档案、虚拟环境和渲染后的配置示例，然后因缺少正式配置而安全退出：

```bash
./scripts/install_macos.sh
```

将生成的 `worker.toml.example` 复制为 `worker.toml`，填写 GitHub 登录名、数字 App ID 和数字 Installation ID，并启用 `discover_installation_repositories = true`。`merge_mode` 默认且首次上线必须保持手动：

```toml
merge_mode = "manual"
```

升级旧安装时保留已有 `[[repositories]]`，先启用发现并验证，再逐步移除静态项。Worker 只发现 App installation 返回、默认分支配置有效且 `worker_github_app_id` 匹配本机 App 的仓库。验证 `codex_path` 与本机实际路径一致。

若 Mac mini 使用可信的本地 HTTP(S) CONNECT 代理，在 `worker.toml` 中明确配置：

```toml
git_proxy_url = "http://127.0.0.1:7897"
```

该地址只用于 Worker 自己的 Git 网络命令，不会传给 Codex、preparation 命令或 GitHub API 客户端。代理 URL 不允许包含用户名或密码；留空即关闭。由于 LaunchDaemon 不自动继承 macOS 图形界面的系统代理，仅设置“系统设置”中的代理并不能让 Worker Git 使用它。

再次运行安装：

```bash
./scripts/install_macos.sh
codex-worker --config "$HOME/Library/Application Support/CodexWorker/config/worker.toml" --check-config
./scripts/doctor_macos.sh
```

先以 manual 完成冒烟验证。只有仓库已采用被识别的单所有者 automatic Ruleset、Worker 身份与所有门禁验证通过后，才把 Mac mini 本地配置改为 `merge_mode = "automatic"`，再次运行 `--check-config` 并重启 Worker。两者缺一不会自动合并；仓库中的任务或源码不能修改这项本地可信配置。

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
10. manual 演练：在 MacBook 执行 `codexctl task review ISSUE_URL`，明确批准当前 PR/head/fingerprint 后执行 `codexctl task merge ISSUE_URL --expected-head SHA --expected-fingerprint FINGERPRINT`；确认 Worker 之后才关闭 Issue 并标记 `codex:completed`。
11. 在测试仓库制造 push 瞬态失败，确认 Worker 在 push 前保存 delivery checkpoint；批准 retry 后只复用原提交、重新验证并创建一个 Draft PR，不产生第二次 Codex run。
12. 在 retry 的验证、push 和 PR 边界分别杀死 Worker，确认 pending command 恢复、相同 head 分支对账且没有重复 PR；已执行 command ID 不会重放。
13. automatic 演练：切换本地 `merge_mode = "automatic"` 与仓库 automatic Ruleset，确认验证后的 Draft PR 进入 `codex:merging`，Worker 在当前 main 上复核并 squash merge 精确 head，不创建人工 review、不再次运行 Codex，并在确认远端合并后标记 `codex:completed`。
14. 推进 main 后重复 automatic 演练，确认 Worker 最多做两次集成刷新、重新运行仓库验证；冲突、Checks 失败、unresolved thread、身份或 Ruleset 漂移均安全停止。

通过全部演练后，才为 EaseWise 安装 App。先连续执行至少十个低风险任务并观察两周；之后每次只增加一个仓库。
