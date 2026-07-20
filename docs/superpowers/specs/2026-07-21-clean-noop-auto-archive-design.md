# Scheduled clean-noop 自动归档设计

## 目标

保持三个独立 Scheduled Slot 的 10 分钟轮询和独立对话模型，同时避免没有任务时产生的 `clean-noop` 对话长期占据 Codex App 侧边栏。

## 已确认行为

| Picker outcome | 自动归档当前对话 | 原因 |
|---|---:|---|
| `clean-noop` | 是 | 没有候选任务且 `maintenance_actions` 为空，不需要人工查看 |
| `maintenance` | 否 | 已执行状态修复或留下维护证据，需要保留审计记录 |
| `preview` / `error` | 否 | 测试结果或错误需要可见 |
| `claimed` | 否 | 真实任务必须保留全过程 |
| `blocked` | 否 | 异常和人工决策点必须保留 |

`clean-noop` 在报告原始 `reason` 和空的 `maintenance_actions` 后，调用 Codex App 支持的 `set_thread_archived`，传入 `archived: true` 且不指定 `threadId`，归档当前 Scheduled run，然后停止。

## 不变项

- 三个 Slot 仍每 10 分钟运行，起始时间继续错开。
- Slot 名称、项目、Prompt 的其余执行边界不变。
- 模型和推理强度不变。
- Slot 1 的启用状态以及 Slot 2、3 的暂停状态不变，除非用户另行要求。
- 不直接修改 Codex App 内部文件或数据库。
- 历史对话的一次性清理由支持的归档操作完成，不属于每次轮询的执行逻辑。

## 验证

- 先用旧 Prompt 对 `maintenance` 场景做基线测试，确认其会错误归档。
- 用自动化测试锁定结果矩阵和正式归档工具参数。
- 更新 Prompt、Scheduled 参考、README 和技能校验器。
- 用新 Prompt 对相同 `maintenance` 场景复测，确认保留对话；再用 `clean-noop` 场景确认只归档当前空运行。
- 运行完整测试、仓库技能校验器和官方 skill validator。
