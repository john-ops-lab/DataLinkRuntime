# 事件订阅：公共管理和可靠接收

当前分支默认关闭：`DLR_EVENT_SUBSCRIPTIONS_ENABLED=false`。B2 已接入 Kafka 连接器和隔离协议探针，公开可启用来源仍为空；`GET /api/event-subscriptions/capabilities` 返回实际开关及已交付来源。内部“已实现、可测试连接”的注册表与公开“可启用”的注册表分开。Kafka 须完成 B3 的管理交互、真实业务读回和容量验收后才开放；Kubernetes 按 B4/B5 单独推进。连接测试成功不代表来源已交付或业务处理成功。

## 管理与权限

一条未删除订阅绑定一个 Task/manual Adapter。读取沿用 Adapter view/read 权限，变更需要 edit，绑定新的源凭据沿用 owner/admin 权限。列表先按权限过滤再计数、分页；只有凭据引用进入配置，密码、Token 和私钥不能放进来源配置。

路径前缀为 `/api/event-subscriptions`：

| 方法与路径 | 用途 |
| --- | --- |
| GET `/capabilities` | 当前开关和真实可用来源 |
| GET/POST 根路径 | 按权限列出或创建默认停用的订阅 |
| GET/PATCH/DELETE `/{id}` | 详情、配置/名称描述修改、停用排空后删除 |
| POST `/{id}/enable`、`/{id}/disable` | 启停接收；停用保留已接管责任 |
| POST `/{id}/test-connection` | 对内部已实现来源作有界连接诊断；需要开关、edit 权限及版本，不创建业务执行 |
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

## Kafka 配置与连接诊断（B2）

当前客户端固定为 `kafka-python==3.0.11`。隔离探针以 Apache Kafka 4.1.2、单节点 KRaft 为基线；这不代表任意 broker 版本、多节点高可用或所有故障组合已经验收。

| 配置 | 规则 |
| --- | --- |
| `cluster_id` | 填 broker 实际返回的稳定 cluster ID；连接时必须匹配，不能任意命名 |
| `bootstrap_servers` | 1–16 个明确的 `host:port`，不包含用户名、密码或 URL 路径 |
| `topics` | 1–16 个精确 topic 名，无 regex；一个 epoch 累计最多 16 个 partition |
| `group_id` | 使用本订阅专用组；省略时生成独立组，克隆也生成新组，不复制位置 |
| `initial_position` | 显式选 `earliest` 或 `latest`；只在该 epoch 的 partition 首次建立 checkpoint 时解析 |
| `security_protocol` | `SSL`（默认）、`SASL_SSL` 或隔离测试所用 `PLAINTEXT` |
| `sasl_mechanism` | `SASL_SSL` 使用 `SCRAM-SHA-256` 或 `SCRAM-SHA-512`；其他协议不接受此字段 |
| `credential_id` | Secret Store 引用；SCRAM 凭据含 `username`、`password`，真值不写入配置 |
| `ca_pem` | 可选公开 CA PEM，缺省沿用系统信任；保留证书及主机名校验，不接收私钥 |
| `mode`、`ordering` | 固定 `per_event`、`partition_serial` |
| `limits` | 只能收紧已知配额，不能借配置放宽全局上限 |

诊断需要读取 cluster/topic 元数据及组状态的权限；运行还需要所选 topic 和专用组的消费、位置提交权限。现有外部成员或同源同组占用会被拒绝，不能通过共享组悄悄分流。空闲组也不意味着其旧提交位置可以覆盖 DLR checkpoint。

开关开启后，连接测试可检查已实现的 Kafka，即使公开可启用来源尚为空。请求携带 `expected_config_version`；返回经验证的 `source_type`、`cluster_id`、topic/partition 数及组可用性。它不加入消费组、不提交 offset、不写 checkpoint，也不执行 Adapter。数据库准备及凭据解密在线程池内完成，来源 RPC 使用独立预算；诊断结果不包含源地址或凭据真值。

B2 隔离探针已覆盖 TLS + SCRAM 两种机制、错误密码、不受信 CA，以及凭据轮换后的同 epoch/checkpoint 恢复。事务探针用真实提交和中止记录核对 `read_committed` 可见性及合法 offset 空洞。探针通过不替代 B3 业务和容量验收；SASL_PLAINTEXT、OAuth、GSSAPI、客户端证书认证及其他 broker/platform 组合不在本批已验范围。

### 诊断码与固定客户端边界

| 错误码 | 处理含义 |
| --- | --- |
| `source_credential_invalid` / `source_credential_unavailable` | 检查机制、Secret Store 字段、引用及解密能力 |
| `source_authentication_failed` / `source_authorization_failed` | 已观察到确定的认证/授权错误，进入 `blocked`，修正后显式 resume |
| `source_tls_failed` | 已观察到 TLS/证书错误；检查信任链及主机名，不能关闭校验绕过 |
| `source_temporarily_unavailable` / `source_connection_timeout` | 网络或外围等待未完成；单凭超时不能判断密码错误 |
| `source_protocol_unsupported` | 客户端/协议或受测内部接口不满足约束，进入 `blocked` |
| `source_identity_unavailable` / `source_identity_changed` | 无法取得非零 topic UUID，或 cluster/topic 身份变化；核对来源并审查是否需新 epoch |
| `source_checkpoint_ahead` / `source_history_gap` | Broker 位置超前或持久位置不在可读范围；保留责任，人工审查后显式恢复 |
| `source_close_unconfirmed` | 原生关闭未确认，保留 owner/名额，禁止另起替补掩盖未回收连接 |

3.0.11 的外围 bootstrap 超时可能遮蔽底层认证/TLS 异常。连接器通过受测的 `KafkaNetClient` / `ObservedConnectionManager` 内部适配读取 typed 错误，回调只保留有界机器码；缺失或不兼容的接口按 `source_protocol_unsupported` 阻塞。此依赖随精确版本固定，升级客户端必须重新验证错误观察和关闭回收，不能只更新版本号。未观察到确定 typed 错误时，外围 timeout 仍归为暂不可用，不一概归为认证失败。SDK 原始调试日志不进入平台普通日志。

## Kafka 位置、停止与恢复

T1 提交完整责任后，才提交该记录的安全下一位置 `record.offset + 1`。`enable_auto_commit=false`、`auto_offset_reset=none`，不会直接把 `consumer.position()` 当安全位置：该位置可能已跨过尚未证明接管的数据、事务控制记录或中止记录。合法空洞按实际读到的记录解释，不虚构缺失消息。Kafka commit 说明 DLR 已持久接管；T2、Execution 成功和目标实际写入是后续不同事实。

首次 earliest/latest 起点先落库；重启和 rebalance 读取已有 checkpoint，不重新解释 latest。新增 partition 按同一首次起点配置解析一次并持久化。Broker 提交落后时可补确认已有责任；未经显式新 epoch 授权，提交位置超前不得推动 DLR 跳过数据。每 partition 仅允许一个未完成业务 Delivery，`retry_wait/dead_letter/cancelled/expired` 均不放行后项；另一 partition 可继续竞争现有 Adapter Slot。

元数据必须提供匹配的 cluster ID 和非零 topic UUID，缺失时不自动退回只按 topic 名去重；同名重建导致 UUID 改变时阻塞。接管非空 poll 前会刷新 UUID，但当前 SDK Fetch 最大使用 v12，没有请求级 topic UUID fence，因此不承诺所有删除重建竞争窗口都能被识别。

checkpoint 保守记录实际消息的 `offset + 1`，不会因为尾部控制/中止 marker 自动推进到客户端位置。若 retention 已清理这些位置，即使没有证明业务消息丢失，也可能返回 `source_history_gap`。运维需核对源保留窗口、旧 checkpoint 和目标业务记录，不能把该码直接解释为确定丢失或静默 reset。

恢复步骤：

1. 区分 `desired_state`、`connection_state` 与 `delivery_state`，核对错误、源身份和 pending/失败屏障。
2. 停用先提交新 generation，拒绝旧 owner 的新接收，再等待原生连接实际关闭；已接管责任继续排空。不要把 HTTP 等待取消、lease 过期或关闭开关视为连接已经回收。
3. 自动重连，或 `desired=enabled` 的 blocked 状态经修正后显式 resume，保持原 epoch、checkpoint 和激活。来源凭据在每次创建连接时读取同一引用的当前真值，轮换不复制 Secret 到历史。已停用的订阅须先排空再 enable，此时创建新激活；若要更换凭据引用或运行配置，也须先停用排空、保存后再启用。旧责任仍引用原激活。
4. 对历史缺口、范围/起点变化或身份切换，先处理或显式处置旧责任，再携带版本、`request_id` 和原因执行 reset，选择 earliest/latest 并建立新 epoch。旧责任、checkpoint 和审计保留；新 epoch 可能重读或跳过源历史，目标仍需业务幂等。
5. 目标结果不明时按同业务 ID 补处理并独立读回；参考[普通 Python 示例](../../samples/event-subscriptions/README.md)。不要通过改 Kafka 提交位置伪造业务成功。

### 监听预算与关闭

Control 共享最多 16 个来源 owner/连接测试名额，无等待队列，每订阅最多一个实际 owner。SDK 的内部 I/O 线程和连接另计；来源等待不占普通 Worker Slot、默认 API 线程池或事件循环。应用缓冲共享 64 MiB、每 owner 2 MiB；原始项和最终 Envelope 还受各自及 Execution JSON 大小限制。原生 fetch/frame 限额不构成压缩展开或进程 RSS 的硬上界，容量仍需 B3 实测。

数据库租约 30 秒、独立 10 秒续租。暂时连接错误按 1–60 秒指数退避并抖动，容量重试按 1–30 秒且恢复需满足低水位；确定的认证、权限、身份错误等待显式处置。HTTP 连接测试默认最多等待 15 秒，超时或取消只结束等待，原生调用实际返回并关闭后才归还名额；关闭失败则保留名额，重复 enable 不能绕过。

Consumer 关闭时禁用自动提交，显式 commit/Consumer close 使用 3 秒客户端预算，普通 Consumer 请求预算为 10 秒。3 秒不等于整个停止过程或任意 OS/NSS 的绝对上界，也不涵盖 DNS 解析和多个顺序 RPC；解析器与平台须符合 B0 的已验证支持边界。功能关闭停止新接收，当前 Control 继续排空既有责任；不能回退旧应用或删表来清除积压。

## 迁移与应用回退

数据库使用向前迁移 `0047_issue219_subscriptions`，增加订阅结构并扩展 trigger，既有 Adapter 和输入不自动转换。首次部署先停止接收/写入、备份并验证可读，再运行迁移并更新 Control/Web；现有 protocol-v3 JSON Worker 无须增加输入类型。

回退旧应用前必须停用全部订阅，确认监听实际回收，处理或明确保全所有 pending、非终态 Execution 和失败屏障。开关关闭仍允许已经接管的责任排空。不能把“关闭功能开关”“容器健康”或“内部队列为空”单独当作责任已清空；应核对事件、执行和持久计数。保留新表和审计，不自动数据库 downgrade/restore。迁移在已有订阅历史时拒绝降级，避免删除已确认责任。

本地开发测试的入口、Compose 参数、凭据、备份和原始验证结果保存在私有配置/证据目录，不能提交到仓库。产品容量与实际来源支持范围以各来源验收记录为准。
