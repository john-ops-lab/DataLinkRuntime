# 事件订阅：公共管理和可靠接收

当前分支的公共底座默认关闭：`DLR_EVENT_SUBSCRIPTIONS_ENABLED=false`。B1 不开放任何来源；`GET /api/event-subscriptions/capabilities` 返回实际开关和已交付来源。Kafka、Kubernetes 需分别完成对应连接器和真实业务验收，不能用连接成功或本页的 API 声明代替可用性。

## 管理与权限

一条未删除订阅绑定一个 Task/manual Adapter。读取沿用 Adapter view/read 权限，变更需要 edit，绑定新的源凭据沿用 owner/admin 权限。列表先按权限过滤再计数、分页；只有凭据引用进入配置，密码、Token 和私钥不能放进来源配置。

路径前缀为 `/api/event-subscriptions`：

| 方法与路径 | 用途 |
| --- | --- |
| GET `/capabilities` | 当前开关和真实可用来源 |
| GET/POST 根路径 | 按权限列出或创建默认停用的订阅 |
| GET/PATCH/DELETE `/{id}` | 详情、配置/名称描述修改、停用排空后删除 |
| POST `/{id}/enable`、`/{id}/disable` | 启停接收；停用保留已接管责任 |
| POST `/{id}/test-connection` | 验证已交付来源的连接能力，不创建业务执行 |
| POST `/{id}/clone` | 绑定到另一可编辑 Adapter，默认停用且不复制进度 |
| POST `/{id}/reset` | 显式原因及请求 ID，停用排空后建立新 epoch |
| POST `/{id}/repair/enter`、`/repair/revisions`、`/repair/exit` | 带积压的受限代码修复 |
| GET `/{id}/events`、`/{id}/events/{receipt_id}` | 事件、原执行和补处理链 |
| POST `/{id}/events/{receipt_id}/replay`、`/skip` | 幂等补处理或明确记录未处理跳过 |
| GET `/{id}/quota-audit`、POST `/{id}/quota-repair` | 只读额度核对，或停用并确认监听关闭后的显式修复 |

影响运行的操作携带 `expected_config_version`；过期返回 `subscription_config_conflict`。`replay/skip/reset` 还要求 `request_id` 和非空 `reason`，同一请求不能改变语义后重用。名称、描述可独立更新。事件列表使用 `before_id` 游标，订阅列表同样有界分页。

普通手动测试继续使用 Adapter 已保存 InputConfig。订阅启用、停止中、排空或 repair 时，后端拒绝修改运行配置与普通触发；存在绑定时改变 Task 运行方式或删除 Adapter 前应先显式删除停用排空的订阅。

## 接收、版本与故障恢复

接收事务 T1 一次提交完整 Envelope、责任、占用及安全位置，之后来源才能确认；调度事务 T2 一次提交 Admission、Execution、Outbox、关联与 pending 计数交接。内部队列暂不可用时责任留在 pending。监听租约由数据库时间和 generation 防止旧 owner 写入，连接实际回收前不释放监听名额。

事件执行沿用 JSON 协议，触发标记为 `subscription`；公开快照为 `{source_type: json, revision: null}`。事件不修改 Adapter 当前输入，执行和默认补处理固定原激活 Revision、Worker 与凭据引用，凭据真值不属于快照。普通 Execution replay 拒绝订阅，须从独立订阅动作操作。

repair 先停用并关闭来源，暂停新派发并等已有非终态执行收敛，再保存只改变代码/依赖的修复 Revision。明确选择修复 Revision 补处理阻塞事件，成功后退出 repair。旧激活和后续 pending 保持原版本，不能批量悄悄替换。

pending、原文历史、去重和接管速率分别有界；触限后等待低水位，不通过提前删除留存期内数据腾空间。原文默认 7 天、去重元数据 30 天，未完成、失败屏障和批次引用额外保护；内容过期返回 `event_payload_expired`。逻辑配额不等于物理数据库和日志占用。

后台按有界批次只读核对占用，不自动修改责任或计数。发现计数漂移后，编辑者可停用并确认监听实际关闭，再携带版本、请求 ID 和原因执行 `quota-repair`；它从持久责任重算本订阅账本，并重建共享聚合。允许保留 pending，避免错误计数阻止排空后又无法修复；操作不重写事件或 Execution。

## 迁移与应用回退

数据库使用向前迁移 `0047_issue219_subscriptions`，增加订阅结构并扩展 trigger，既有 Adapter 和输入不自动转换。首次部署先停止接收/写入、备份并验证可读，再运行迁移并更新 Control/Web；现有 protocol-v3 JSON Worker 无须增加输入类型。

回退旧应用前必须停用全部订阅，确认监听实际回收，处理或明确保全所有 pending、非终态 Execution 和失败屏障。开关关闭仍允许已经接管的责任排空。不能把“关闭功能开关”“容器健康”或“内部队列为空”单独当作责任已清空；应核对事件、执行和持久计数。保留新表和审计，不自动数据库 downgrade/restore。迁移在已有订阅历史时拒绝降级，避免删除已确认责任。

本地开发测试的入口、Compose 参数、凭据、备份和原始验证结果保存在私有配置/证据目录，不能提交到仓库。产品容量与实际来源支持范围以各来源验收记录为准。
