# MacBook 主开发端安装

MacBook Codex agent 是主开发代理（principal development agent）：负责产品设计、技术探索，也可以直接开发；它可自行决定是否把当前已授权父目标中的严格子任务交给 Mac mini 连续执行。Mac mini 是有界执行端，不能继续拆任务。

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

在 Codex 中可要求：“使用 `$dispatch-codex-task` 把这个明确的小改动整理成 Mac mini 任务。”skill 会检查 context 已 commit/push、读取项目边界、检查 active path ownership，并拒绝过大、高风险或与 MacBook 开发路径冲突的任务。

独立派单请求会先展示最终规格等待确认。若 MacBook agent 正在执行一个已经授权的父开发目标，并把其中低/中风险、路径不冲突且验收明确的严格子集委派出去，则可使用 `codexctl task create --yes`，不重复请求同一确认。

该 skill 和 `codexctl` 都不使用 Goal/“目标”模式，也不会自动部署生产。GitHub CLI 的个人 token 始终留在 MacBook；Mac mini Worker 只使用独立 GitHub App，两者凭据不要混用。

## 仓库接入与一次性批准

先运行 `codexctl repo status OWNER/REPO`。未接入时，在 `project.toml` 使用 `schema_version = 2`，并写入与 Mac mini `worker.toml` 一致的数字 `worker_github_app_id`，再用 `codexctl repo onboard --repo OWNER/REPO --project-config project.toml` 创建接入 PR；只有明确批准该 PR 后，才运行 `codexctl repo finalize OWNER/REPO#PR --expected-head SHA`。`awaiting-worker` 表示默认分支已配置、正在等待指定 GitHub App 的 Mac mini 探针证明访问能力；变为 `ready` 后才能派单。旧 v1 配置必须先升级；保留的 v1 任务不继续执行或修订，应在升级后重新派单。

仓库使用手动模式时，Worker 交付后先运行 `codexctl task review ISSUE_URL`。它只读并显示门禁与审批指纹。只有针对当前 PR、head SHA 和 fingerprint 的 explicit approval，才能运行 `codexctl task merge ISSUE_URL --expected-head SHA --expected-fingerprint FINGERPRINT`。设计通过、旧对话或任何 future PR 授权都无效。

仓库使用自动模式时，必须由 Mac mini 本地 `merge_mode = "automatic"` 和仓库 automatic Ruleset 同时授权。Worker 只会合并自己创建且重新验证过的精确 head；MacBook 观察 `codex:merging`/`codex:completed` 和测试机结果即可。生产部署与回滚不在该授权内。
