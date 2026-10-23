# 事件订阅：公共管理和可靠接收

当前分支默认关闭：`DLR_EVENT_SUBSCRIPTIONS_ENABLED=false`。B3 候选已注册 Kafka 公共入口，打开开关时 `GET /api/event-subscriptions/capabilities` 返回 `kafka`；本批完整验收状态见 OpenSpec 批次记录，注册不等于验收完成。内部“已实现、可测试连接”的注册表与公开“可启用”的注册表分开。Kubernetes 按 B4/B5 单独推进。连接测试成功不代表业务处理成功。

## 管理与权限

一条未删除订阅绑定一个 Task/manual Adapter。读取沿用 Adapter view/read 权限，变更需要 edit，绑定新的源凭据沿用 owner/admin 权限。列表先按权限过滤再计数、分页；只有凭据引用进入配置，密码、Token 和私钥不能放进来源配置。

路径前缀为 `/api/event-subscriptions`：

| 方法与路径 | 用途 |
| --- | --- |
| GET `/capabilities` | 当前开关和真实可用来源 |
| GET `/runtime-status` | 管理员读取当前 Control 进程的监听数、应用缓冲占用/峰值/上限 |
| GET/POST 根路径 | 按权限列出或创建默认停用的订阅 |
| GET/PATCH/DELETE `/{id}` | 详情、配置/名称描述修改、停用排空后删除 |
| POST `/{id}/enable`、`/{id}/disable` | 启停接收；停用保留已接管责任 |
| POST `/{id}/test-connection` | 对内部已实现来源作有界连接诊断；需要开关、edit 权限及版本，不创建业务执行 |
| POST `/{id}/clone` | 绑定到另一可编辑 Adapter，默认停用且不复制进度 |
| POST `/{id}/reset` | 显式原因及请求 ID，停用排空后建立新 epoch |
| POST `/{id}/repair/enter`、`/repair/revisions`、`/repair/exit` | 带积压的受限代码修复 |
| GET `/{id}/events`、`/{id}/events/{receipt_id}` | 事件、原执行和补处理链 |
| GET `/{id}/checkpoints` | 当前 epoch 的安全/确认位置、失败屏障与未接管故障，只读且有界分页 |
| GET `/{id}/activations/{activation_id}` | 原激活版本、Worker 和凭据引用元数据，不解密 Secret |
| GET `/{id}/events/{receipt_id}/replay-context` | 只读比较原/当前激活引用并生成并发校验指纹 |
| POST `/{id}/events/{receipt_id}/replay`、`/skip` | 幂等补处理或明确记录未处理跳过 |
| POST `/{id}/skip-unaccepted` | 显式跳过所选 Kafka 未接管故障，由服务端推导安全下一位置 |
| GET `/{id}/quota-audit`、POST `/{id}/quota-repair` | 只读额度核对，或停用并确认监听关闭后的显式修复 |

影响运行的操作携带 `expected_config_version`；过期返回 `subscription_config_conflict`。`replay/skip/reset` 还要求 `request_id` 和非空 `reason`，同一请求不能改变语义后重用。名称、描述可独立更新。事件列表使用 `before_id` 游标，订阅列表同样有界分页。

详情分别展示期望、连接和交付状态。`event_counts.accepted` 是当前仍留存的 Receipt 数，`replayed` 是仍留存的后续 Delivery 数，都不是终身累计计数。`limits` 是本订阅解析后的限制，`global_limits` 是共享上限；配额中的控制预留单独列出。checkpoint 的 `source_lag=null` 表示未知，安全位置与已确认位置之差不能当成 broker 消息积压。诊断 API 不执行来源 RPC，也不创建租约或额度记录。

事件的 `activation_id` 指向原激活；每次 Delivery 的 `effective_activation_id` 反映原激活或显式当前配置补处理所用的覆盖激活，`revision_override_id` 反映受限修复 Revision。比较版本时应检查这一条 Delivery，不能把原激活当成所有后续补处理的实际配置。凭据使用位置包含保留的来源配置与激活引用，仅返回 ID 和绑定字段，不返回真值。

“使用当前配置”只在停用且无其他未完责任时允许，确认前读取 `replay-context` 比较 Revision、固定 Worker、配置版本/epoch 和凭据引用。提交时携带 `use_current_configuration=true` 与该预览的 `expected_preview_token`；服务端在同一 Adapter 锁内重新校验，期间任一引用变化返回 `subscription_config_conflict`，不会按未展示的新配置执行。指纹用于并发校验，不代替权限。提交结果不明时重试同一请求应保留版本、请求 ID 和指纹；若要刷新预览或改变选择，先核对旧请求结果，再开始新请求。固定 Worker 不可用时不能静默改为自动选择。

普通手动测试继续使用 Adapter 已保存 InputConfig。订阅启用、停止中、排空或 repair 时，后端拒绝修改运行配置与普通触发；存在绑定时改变 Task 运行方式或删除 Adapter 前应先显式删除停用排空的订阅。

## 接收、版本与故障恢复

接收事务 T1 一次提交完整 Envelope、责任、占用及安全位置，之后来源才能确认；调度事务 T2 一次提交 Admission、Execution、Outbox、关联与 pending 计数交接。内部队列暂不可用时责任留在 pending。监听租约由数据库时间和 generation 防止旧 owner 写入，连接实际回收前不释放监听名额。

事件执行沿用 JSON 协议，触发标记为 `subscription`；公开快照为 `{source_type: json, revision: null}`。事件不修改 Adapter 当前输入，执行和默认补处理固定原激活 Revision、Worker 与凭据引用，凭据真值不属于快照。普通 Execution replay 拒绝订阅，须从独立订阅动作操作。

repair 先停用并关闭来源，暂停新派发并等已有非终态执行收敛，再保存只改变代码/依赖的修复 Revision。明确选择修复 Revision 补处理阻塞事件，成功后退出 repair。旧激活和后续 pending 保持原版本，不能批量悄悄替换。

pending、原文历史、去重和接管速率分别有界；触限后等待低水位，不通过提前删除留存期内数据腾空间。原文默认 7 天、去重元数据 30 天，未完成、失败屏障和批次引用额外保护；内容过期返回 `event_payload_expired`。逻辑配额不等于物理数据库和日志占用。

后台按有界批次只读核对占用，不自动修改责任或计数。发现计数漂移后，编辑者可停用并确认监听实际关闭，再携带版本、请求 ID 和原因执行 `quota-repair`；它从持久责任重算本订阅账本，并重建共享聚合。允许保留 pending，避免错误计数阻止排空后又无法修复；操作不重写事件或 Execution。

## Kafka 配置与连接诊断

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

开关开启后，连接测试检查已实现的 Kafka。请求携带 `expected_config_version`；返回经验证的 `source_type`、`cluster_id`、topic/partition 数及组可用性。它不加入消费组、不提交 offset、不写 checkpoint，也不执行 Adapter。数据库准备及凭据解密在线程池内完成，来源 RPC 使用独立预算；诊断结果不包含源地址或凭据真值。

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

超限等故障若尚未进入 Inbox，checkpoint 的 `blocked_unaccepted` 提供有界错误和源事件身份。审查后可用 `skip-unaccepted` 提交 `shard`、`source_event_id`、版本、请求 ID 和原因；服务端只接受当前 epoch 的匹配故障，不接受任意目标 offset。操作记录为 `skipped_without_processing`，不会伪造 Receipt、Execution 或目标成功。来源恢复时先确认这个持久安全位置，再继续接收，因此末条毒消息也不依赖下一条正常消息才提交位置。相同请求重试须保持操作者与语义一致。

### 监听预算与关闭

Control 共享最多 16 个来源 owner/连接测试名额，无等待队列，每订阅最多一个实际 owner。SDK 的内部 I/O 线程和连接另计；来源等待不占普通 Worker Slot、默认 API 线程池或事件循环。应用缓冲共享 64 MiB、每 owner 2 MiB；原始项和最终 Envelope 还受各自及 Execution JSON 大小限制。原生 fetch/frame 限额不构成压缩展开或进程 RSS 的硬上界，容量仍需 B3 实测。

数据库租约 30 秒、独立 10 秒续租。暂时连接错误按 1–60 秒指数退避并抖动，容量重试按 1–30 秒且恢复需满足低水位；确定的认证、权限、身份错误等待显式处置。HTTP 连接测试默认最多等待 15 秒，超时或取消只结束等待，原生调用实际返回并关闭后才归还名额；关闭失败则保留名额，重复 enable 不能绕过。

Consumer 关闭时禁用自动提交，显式 commit/Consumer close 使用 3 秒客户端预算，普通 Consumer 请求预算为 10 秒。3 秒不等于整个停止过程或任意 OS/NSS 的绝对上界，也不涵盖 DNS 解析和多个顺序 RPC；解析器与平台须符合 B0 的已验证支持边界。功能关闭停止新接收，当前 Control 继续排空既有责任；不能回退旧应用或删表来清除积压。

## B3 验证状态与容量边界

B3 已通过独立验收，Kubernetes 继续按 B4/B5 推进。匹配部署的 `10ec0f4` 已完成真实 Chrome 创建、无入组连接诊断、启停与实际关闭、Kafka 消息到目标、带同 partition 积压的受限 repair、skip、当前配置补处理和 reset。独立核对保留原失败终态，四条目标业务各有效写入一次，skip 无目标写入；reset 保留旧 Receipt/Delivery/checkpoint，Saved Input 不变且未重新启用。仅有一个 Adapter 读取权限的账号只看到授权订阅且无写入口，已打开事件详情可随轮询更新。普通 manual、实际 schedule、Webhook 三条真实 Worker 路径的 C14 兼容验证已通过。

浏览器另发现订阅锁定时普通 RunOnce 按钮及 Task 状态文案不一致；后端负测拒绝普通触发且未创建 Execution。`d48e3f8` 的 Web 窄修已通过组件、独立审查及匹配部署的双语 Chrome 窄复验：目录和 Header 显示订阅锁定，RunOnce 实际禁用，保存、Input 和 Worker 仍锁定；普通 manual Adapter 的运行按钮保持可用，未重复执行。新 epoch 的 latest 再次启用与停用未改变旧责任及目标唯一写入；运行版本另有逐哈希核对。

容量首轮使用同一个普通 Python Adapter、四个 partition、两个 Worker 执行槽及既有 1 秒 RabbitMQ 最小重投延迟：1,800 秒输入 1,800 条，持续阶段目标完成 1,732 条，约 0.962 条/秒，积压增长，因此持续 1 条/秒门槛不通过。停产后 1,800/1,800 完整收敛不改变首轮结论。仅按该次样本估算，Control 物理库增加约 14.81 kB/事件；持续 1 条/秒的线性投影不是已达到的吞吐或留存稳态。默认 10,000 条历史额度在该假设速率下约 2 小时 46 分 40 秒触限，不构成七天持续接收承诺。

既有 `DLR_RABBITMQ_RETRY_BASE_SECONDS=0.1` 已在停止应用、连接/消费者与全部消息计数连续清零的独占静默窗口下完成空开发队列维护；RabbitMQ 4.3.5 quorum 不支持条件删除，此操作不具备服务端原子空队列保护。产品默认值保持不变。

修订配置固定 `d48e3f8`，同一 Adapter、四个 partition、1 秒 tick、两个配置执行槽，预热另计后，正式约 1,800 秒发送的 1,800 条全部在阶段末写入目标。独立审查逐条核对完整 payload、来源位置和一次有效写入，积压未累计增长，30 分钟稳态门槛通过；发送至目标已提交行 insert 时间的 p95 为 1.98 秒。应用缓冲峰值 3,501 字节，Control RSS 尾十分钟仍有约 32 kB/min 小幅正斜率，不能据此声称无泄漏或任意时长稳定。此结果不外推默认 1 秒重投配置或七天持续运行。有限突发后已完成触限后的 600 秒观察与显式恢复，103 条输入明确分为 71 成功、2 显式跳过、30 因历史满额未接管；观察窗口 API 均满足两倍门槛，但短突发 runtime-status 的 10 个样本 p95 曾为基线 2.604 倍，保留该波动。已接管责任排空、监听关闭；历史满额仍等待合法治理，不加配额或提前删除原文。版本、首败、增长及未验范围见 [B3 审查与验收记录](../../openspec/changes/add-event-subscriptions/b3-review.md)。

## 迁移与应用回退

数据库使用向前迁移 `0047_issue219_subscriptions`，增加订阅结构并扩展 trigger，既有 Adapter 和输入不自动转换。首次部署先停止接收/写入、备份并验证可读，再运行迁移并更新 Control/Web；现有 protocol-v3 JSON Worker 无须增加输入类型。

回退旧应用前必须停用全部订阅，确认监听实际回收，处理或明确保全所有 pending、非终态 Execution 和失败屏障。开关关闭仍允许已经接管的责任排空。不能把“关闭功能开关”“容器健康”或“内部队列为空”单独当作责任已清空；应核对事件、执行和持久计数。保留新表和审计，不自动数据库 downgrade/restore。迁移在已有订阅历史时拒绝降级，避免删除已确认责任。

本地开发测试的入口、Compose 参数、凭据、备份和原始验证结果保存在私有配置/证据目录，不能提交到仓库。产品容量与实际来源支持范围以各来源验收记录为准。
