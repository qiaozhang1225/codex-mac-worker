# MacBook 派单端安装

MacBook 只负责产品设计、技术探索、任务拆解、派单和审查，不承担连续执行。

## 安装

安装 Python 3.12 和 GitHub CLI，并用你自己的 GitHub 账号登录：

```bash
brew install python@3.12 gh
gh auth login
./scripts/install_macbook.sh
```

脚本把 `codexctl` 安装到独立虚拟环境，在 `~/.local/bin/codexctl` 建立入口，并把 `dispatch-codex-task` skill 安装到 `${CODEX_HOME:-$HOME/.codex}/skills/`。确保 `~/.local/bin` 在 PATH 中，然后重启 Codex。

## 验证

```bash
codexctl --help
codexctl task create --help
codexctl task status --help
codexctl repo status --help
codexctl repo onboard --help
codexctl repo finalize --help
codexctl task review --help
codexctl task merge --help
```

在 Codex 中可要求：“使用 `$dispatch-codex-task` 把这个明确的小改动整理成 Mac mini 任务。”skill 会检查 context 已 commit/push、读取项目边界、拒绝过大或高风险任务，并在创建 GitHub Issue 前显示最终规格等待确认。

该 skill 和 `codexctl` 都不使用 Goal/“目标”模式，不会自动部署或自动合并 PR。GitHub CLI 的个人 token 始终留在 MacBook；Mac mini Worker 只使用独立 GitHub App，两者凭据不要混用。

## 仓库接入与一次性批准

先运行 `codexctl repo status OWNER/REPO`。未接入时用 `codexctl repo onboard --repo OWNER/REPO --project-config project.toml` 创建接入 PR；只有明确批准该 PR 后，才运行 `codexctl repo finalize OWNER/REPO#PR --expected-head SHA`。`awaiting-worker` 表示默认分支已配置、正在等待 Mac mini 探针证明访问能力；变为 `ready` 后才能派单。

Worker 交付后先运行 `codexctl task review ISSUE_URL`。它只读并显示门禁与审批指纹。只有针对当前 PR、head SHA 和 fingerprint 的 explicit approval，才能运行 `codexctl task merge ISSUE_URL --expected-head SHA --expected-fingerprint FINGERPRINT`。设计通过、仓库级授权、旧对话或任何 future PR 授权都无效。
