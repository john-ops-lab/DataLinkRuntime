# 双 Control 的后台接管与事件来源

从 W3 第一批 `686f6db768e48a63ca29229a713cf39ecce99a5d` 与 schema `0094_issue129_control_ha` 升级至 `0095_issue129_takeover`。按[停机换镜像流程](remote-worker-wave1a.md)停写并结清活动工作，验证备份和实际进程停止。由同一数据库 owner 保持运行角色 NOLOGIN，核对原声明和转换链，依次执行 `check`、`apply`、`observe`。连续历史链在 `--placement-source`、`--placement-intent` 后追加 `--foundation-source`、`--foundation-intent`。`committed_current` 确认后安装新镜像声明、恢复运行角色，再启动两份同摘要 Control。原部署身份、容量 epoch、已接受回执、额度和结果继续使用原账本。

## 后台职责

| 职责 | 接管依据 | 核对入口 |
| --- | --- | --- |
| Schedule、固定和预留 Retry | 两个 Control 共享持久扫描游标。候选页查询限 1 秒，Schedule 默认每 5 秒检查一次。各节点先提交扫描位置，再按原业务锁序推进。阻塞项在绕回时重试，计划点及下一派发代次由数据库唯一性裁决。 | `test_schedule_trigger`、`test_issue129_control_takeover`、`test_issue130_b2_runtime` |
| Attempt、Reservation | 使用持久扫描进度重新检查已有租约、Slot 和资源。物理清理未结的旧 Worker 继续承担原资源责任。 | `test_issue134_reconciliation`、`test_issue129_worker_reservations` |
| Outbox、批次、完成投影 | Relay 逐行领取 Outbox，使用部署配置的发布租约，默认 60 秒。Broker I/O 在事务外执行。回执写回复核原租约及领取次数。本机通知丢失时，T2 仍按有界轮询扫描，本地轮询等待上限为 1 秒。 | `test_issue130_b1_runtime`、`test_issue222_business_batch`、`test_issue222_scheduler` |
| Admission、配额与拓扑 | Admission 协调数据库游标，配额审计只对账。以 30 秒观察代次协调拓扑成功和失效，节点独立观测。 | `test_issue219_management`、`test_issue129_control_takeover` |
| Infrastructure DLQ | 同一发布尝试和死信重复投递读取原处置结果。数据库事务提交后确认原 channel 的 delivery tag。 | `test_issue152_infrastructure_dlq`、`test_issue129_control_takeover` |
| Retention、缓存与 Adapter 清理 | 按持久责任分批重新核对引用。复用原 request ID、Worker 实例、命令租约和独占删除证据。 | `test_retention`、`test_issue161_cache_governance`、`test_issue173_adapter_cleanup` |

`GET /api/health` 的 `control_ha.cluster.background` 给出缺少有效来源 owner 的启用订阅数、最近续租距今最久的有效 owner 年龄、过期 held 预留数和超过恢复等待期的 reclaiming 预留数。来源租约为 30 秒，续租间隔为 10 秒。监控可在无有效 owner 的订阅数持续非零超过 60 秒，或过期 held 连续两轮对账仍未收敛时告警。检查告警应同时核对原来源、执行或维护票据和 Worker 的物理关闭证据。每个节点的 `live` 和 `ready` 继续使用[基础部署约定](control-ha-foundation.md)。

## 来源恢复

来源实例使用数据库租约和 generation。普通接管保留 `source_epoch`，显式重置按[来源合同](event-subscriptions.md)重新绑定。新接收记录区分实际接收节点 UUID 与原来源租约 owner 和 generation。历史回执的接收节点保持未知。

- Kafka 以持久安全下一 offset 和当前 assignment 恢复。核对已接管位置、Broker 确认和数据库 confirmed position，重平衡后按新 assignment 继续。
- RabbitMQ 在本次交付的 channel 确认。重投核对原消息身份并使用新 channel 的 delivery tag。
- RocketMQ 在专用消费组以本次交付的 receipt handle 确认。Broker 重投命中原 Receipt 后按新 handle 结算。
- Kubernetes WATCH 从持久 resourceVersion 恢复，`410 Gone` 形成 gap 后进入原重新 LIST 流程。未完成的初始 LIST 保留 partial，只有已验证的完整同步和完成链可以授权目标清理。
- HTTP 可由任一合格节点接收。新 ID 校验当前有效来源租约；旧 ID 和 Receipt 查询继续沿用原授权、历史回执及内容冲突规则。同一订阅、epoch 和 shard 的 `EventReceipt.sequence` 定义首次接收顺序。每节点独立执行收集、FIFO 和 native/字节预算。

业务批次以原不变的成员关联和 manifest 对账。共享取消、旧终态和成员 Replay 继续使用原纪律。目标已经执行部分写入且平台结果未知时，按业务键核对目标，再决定是否补处理。
