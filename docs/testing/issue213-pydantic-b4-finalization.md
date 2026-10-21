# Issue #213：B4 纠正与最终收尾兼容修正

删除准备期间，把旧私有函数的原断言移到实际 SDK/Agent 路径，发现三个边界漂移；先建立三个实际失败，再修正，没有放宽原断言。独立审查另发现纠正事件索引与现行 Compose 审计合同不一致，两个真实 SDK 轨迹首次失败也单独保留。

| 边界 | 原失败 | 修正后的行为 |
|---|---|---|
| 格式纠正的预算拒绝 | 先消耗一次纠正，再变成安全 fallback，可能把原 502 变为 200 | 在公开 `before_model_request` 完成一次预算和时间检查后才记账；拒绝抛原 `AiResponseInvalid`，纠正次数为 0 |
| 最终收尾预算拒绝 | 已有工具调用上限原因吞掉本次 context-budget 原因 | 本次拒绝原因仅决定 fallback 文案；原主 stop、工具摘要和计数保持 |
| 预算计算耗时 | 网络 timeout 使用预算计算前的剩余时间 | 预算完成后重新读单调时间，超时不发请求；原 transport 绝对 hard deadline 一直有效 |
| 成功纠正的审计关联 | `format_correction` 仍指首次响应索引，纠正 `provider_result` 已指下一索引 | 首次 validation 固定原索引；纠正事件显式指向下一次预算检查，与实际结果一致 |

`correct_format` 仅登记待纠正错误及原响应索引，`finally` 清理；没有增加模型循环、重试或第二次预算计算。零请求的预算拒绝仍记录一次 `provider_budget(status=rejected)`，实际 `payload_bytes=0`，保留预估字段，不伪造 Provider 结果。

## 验证与影响范围

- 新增三项守卫：首轮 **3 failed**，修复后 **3 passed**；增加 pending 清理、拒绝检查索引及预估/实际载荷的断言。
- 相关 SDK、领域、预算、deadline 与输出策略：**124 passed**。
- 把实际成功纠正轨迹交给现行 `validate_correction_audit`：首轮 **2 failed**；索引修正后，相关运行时、审计与 validator **54 passed**。
- 全后端 Ruff、strict Mypy（201 source files）通过；精确提交 CI 继续作为部署门禁。

80 个真实核心配对继续使用冻结 `936fddd`，不覆盖原始结果。修正只涉及预算/收尾准入与审计、timeout 的计算位置，原 Prompt、工具、模型参数、一次纠正上限和 150 秒总时限不变；实际核心的正常输入远低于预算边界。补充媒体/语言、Chrome、回退与最终删除后的真实模型烟测使用通过门禁的后续提交。该记录不能代替 B4/B5 的未完成业务结果。
