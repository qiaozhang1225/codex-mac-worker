# Durable GitHub 返回值规范化设计

## 背景与根因

GitHub 的设置 Issue 标签接口返回标签数组。`DurableGitHub` 在远端写入成功后却无条件调用 `result.get(...)` 提取远端 ID，错误地假设所有 GitHub 写操作都返回字典。结果是标签已经写入 GitHub，但 outbox 未被标记为 delivered；Worker 随后在每次启动时重放该幂等操作并再次崩溃。

## 设计

增加一个只负责提取远端 ID 的内部函数：当结果是字典时读取 `id` 或 `number`，当结果是列表、空值或其他无 ID 响应时返回 `None`。`_write` 与 `flush` 共用该函数，并在没有远端 ID 时照常将 outbox 标记为 delivered。

远端操作的原始返回值仍由首次 `_write` 原样返回；现有评论、PR 和 Issue 的字典响应行为不变。SQLite、任务状态机、重试次数和 GitHub 请求内容均不改变。

## 测试与恢复

先增加两个失败测试：首次 `set_labels` 返回列表时应完成交付；已有 pending `set_labels` 经 `flush` 返回列表时也应完成交付。实现后运行完整测试集。部署到 Mac mini 后，现有 pending 标签记录应通过正常 flush 自动变为 delivered，Issue #7 随后由 Worker 正常领取；不手工修改 outbox。
