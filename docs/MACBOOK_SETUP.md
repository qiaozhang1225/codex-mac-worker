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
```

在 Codex 中可要求：“使用 `$dispatch-codex-task` 把这个明确的小改动整理成 Mac mini 任务。”skill 会检查 context 已 commit/push、读取项目边界、拒绝过大或高风险任务，并在创建 GitHub Issue 前显示最终规格等待确认。

该 skill 和 `codexctl` 都不使用 Goal/“目标”模式，不会自动部署或合并 PR。GitHub CLI 使用你的个人身份派单；Mac mini Worker 使用独立 GitHub App 执行，两者凭据不要混用。
