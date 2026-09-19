# 基础设施事件处置

**简体中文** · [English](../en/incident-dispositions.md)

执行详情中的基础设施事件记录消息投递故障。先修复对应 Broker、Worker、容量或路由问题，
再根据详情页给出的资格处理原执行记录。恢复沿用原 Execution ID、输入和版本，不创建 Replay。
因此恢复后仍须检查原记录的结果、Attempt 与资源清理；提交处置成功不等于任务已经成功。

## 页面操作

具有适配器编辑权限的用户可以处置事件；只读用户可以查看原因和审计，但不能提交操作。
服务端在提交时重新检查权限、消息代次、当前状态和冻结材料。页面展示的资格仅供提示，
任务在此期间被领取、取消或终结时，以提交反馈及刷新后的原记录为准。

| 原记录状态 | 操作含义 |
| --- | --- |
| 当前代次已排队、无 active Attempt，材料完整 | 恢复原记录；已有 pending Outbox 时沿用原投递，只有符合条件的已发布投递才创建新代次 |
| 正在运行 | 恢复不可用；终结发出协作取消请求，须等待 Worker 和清理回执收敛 |
| 已请求取消或正在等待重试 | 恢复不可用；终结仍按既有取消规则处理 |
| 原 Execution 已终态、事件仍开放 | “核实并关闭事件”只关闭事件并记审计，不重写执行结果或增加 Attempt |
| 旧代次事件 | 不能恢复旧消息；终结只忽略该旧事件，不取消当前代次 |
| 消息身份无法核验、原材料缺失或状态冲突 | 拒绝恢复并显示原因；修复后重新读取原记录，不替换历史快照 |

“观测次数”是 DLQ 观察计数，“处置次数”是独立人工处置审计数，
“恢复投递次数”只统计实际创建新代次的恢复。查看详情或重试同一次幂等请求不会增加这些计数。

## API 与重试

详情能力由 `GET /api/executions/{execution_id}/reliable-detail` 返回。
处置使用 `POST /api/executions/{execution_id}/incidents/{incident_id}/dispositions`，
请求头 `Idempotency-Key` 必须是 UUID。请求体只接受：

```json
{
  "action": "recover",
  "expected_generation": 1,
  "reason_code": "capacity_repaired"
}
```

`action` 为 `recover` 或 `terminate`；原因可选 `capacity_repaired`、`routing_repaired`、
`operator_cancel`、`verified_terminal`。使用刚读取的真实 generation，不照抄示例中的 `1`。
操作者身份由认证 Principal 决定，请求不能指定 actor。账户入口沿用现有 Session/CSRF 要求。

网络中断或重复点击后，原意图保持相同 key 和请求体；返回同一不可变 `receipt`，
外层 `execution_status` 可以随任务进展改变。同 key 更换请求体会返回 `409`。
收到状态冲突时刷新原记录，只有明确发起新的处置意图才生成新 key。

`200` 表示该次处置已有确定结果，`202` 表示已请求协作取消；`409` 表示当前资格冲突，
`503` 表示 Outbox 容量不足。检查返回的 `receipt.code`，并在存在时遵守 `Retry-After`。
审计列表使用同一路径的 `GET`，支持 `limit=1..100` 和返回的 `next_before_id` UUID 游标；
读取不会写入处置审计。

## Broker 与升级边界

Worker 满槽时停止领取新消息，正常容量等待不应消耗 `delivery_limit`。
Control 从目标 Worker 队列对应的结构化 `x-death` 分类原因：`delivery_limit` 留待人工处置；
`rejected`、`expired`、`maxlen` 或无法识别的原因仍走原有自动核验流程，只有有效的当前排队
消息才可能回到同代 Outbox。队列配置头本身不能证明发生过投递超限。

迁移 `0040_issue152_dispositions` 增加处置审计，不补造旧人工处置。
带未完成责任的本机预览升级须遵循[本机验收控制器](local-preview.md)的显式保全清单，
保留原数据库、卷、journal 与 Admission。新 Worker 的真实回执完成旧清理后，才能报告清理完成。
不要通过清空 DLQ、删除事件、手改终态或删除持久卷来获得通过结果。
