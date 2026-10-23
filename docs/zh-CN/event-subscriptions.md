# 事件订阅：公共管理和可靠接收

当前分支默认关闭：`DLR_EVENT_SUBSCRIPTIONS_ENABLED=false`。本分支 B5 候选打开开关后，`GET /api/event-subscriptions/capabilities` 返回 `kafka` 与 `kubernetes`。Kafka 已通过 B3，Kubernetes 后端已通过 B4；其 B5 产品/浏览器和剩余真实目标门槛仍待验收，候选可启用不等于最终可交付。内部诊断和公开启用注册表当前包含相同两种来源，连接诊断仍是独立动作，成功不代表业务处理成功。

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

### Kubernetes 批次 API 与 B5 候选状态

以下后端接口已通过 B4。B5 候选在打开总开关后，允许通过普通授权 API 启用 Kubernetes，无需公开测试绕过入口；新建订阅仍默认停用。Web 按 capabilities 展示支持来源，其 B5 操作路径与最终交付状态仍须单独验收。保存配置或连接诊断不会自动开始接收。

| 方法与路径 | 用途 |
| --- | --- |
| GET `/{id}/sync-batches` | 按本订阅权限读取保留批次，`before_id`、`limit=1..100`，可显式按 `source_epoch` 筛选 |
| GET `/{id}/sync-batches/{batch_id}` | 固定激活/epoch、读取与完成事实、待落控制/剩余预留、缺口及至多三种控制责任摘要 |
| POST `/{id}/sync-batches/{batch_id}/abandon-completion` | 专用“放弃完成确认”，记录业务结果未确认，不能用普通 skip 替代 |

订阅的 `latest_batch` 和批次详情独立区分 `read_complete`、`completion_committed`、`completion_abandoned`、`closed`、控制待落账和剩余预留。空批次仍可能有未结责任，不能以没有 Receipt 或 `state=failed` 判断已排空。RV 是不透明字符串；返回结果不包含原页面、continue token、服务器地址、CA 原文或 Secret。只读批次查询不连接来源，关闭功能开关后仍可查看历史。

放弃请求需要 edit 权限、`expected_config_version`、`source_epoch`、完成 `receipt_id`、`expected_latest_delivery_id`、`expected_delivery_ordinal`、必须显式出现且可为 null 的 `expected_execution_id`、固定 `request_id`、非空 `reason` 和 `confirm_impact=true`。仅在停用、原生监听实际关闭且关联执行全终态后允许；最新补处理仍 pending 且没有 Execution 时可原子取消这份未派发责任。执行或新补处理先改变期望关联则冲突，不能取消正在运行的执行。

动作保留原失败、输入和冻结事实，使用既有预算中的单份有界审计，只解除完成屏障；其他失败仍须修复，旧 WATCH 按序排空，不自动启用或新建 LIST，也不能回滚目标副作用。之后旧 completed 不再取得新执行许可。结果不明时保持完整原参数及请求 ID 重试；相同原请求在配置后来合法更新后仍可按当前权限读回原结果，不重复结账。功能开关关闭不阻止结清已有责任。

已成功确认且闭合的 completed 同样拒绝新的原版本/repair/当前配置 replay。原请求按当前权限及完整原参数匹配，先只读回原 Delivery，再对新动作校验版本和资格；之后合法配置升版或功能关闭不阻断该读回，不新增 Delivery、Execution、Outbox 或审计。再次全量对账使用新完整批次。普通对象/WATCH 的补处理规则不变。

Kubernetes 配置冻结逻辑集群、HTTPS origin、有效 CA 信任集合及 GVR/namespace/selectors 的身份；server 不接受附带 API 路径。信任或范围变化须停用结清并显式 `new_epoch`，不能沿旧 RV 静默切换；同一凭据引用的 token 真值轮换保持 epoch。旧配置没有已验证身份快照时也不自动补写，须显式新 epoch 创建不可变配置。

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

## Kubernetes 配置、批次与恢复

候选固定 `kubernetes==35.0.0`、`urllib3==2.8.0`，支持命名空间资源 Pod（`core/v1`）和 Deployment（`apps/v1`），使用 bearer token 引用及经过验证的 TLS。其他资源、任意 API 路径和认证机制不在本支持矩阵内。`cluster_id` 是显式配置、与连接信任和范围绑定的逻辑身份，不是自动发现的系统 namespace UID。

| 配置 | 规则 |
| --- | --- |
| `cluster_id`、`server` | 逻辑集群 ID 与 HTTPS origin；不能含凭据、API 路径、query 或 fragment |
| `group`、`version`、`resource` | 只能是 `""`、`v1`、`pods`，或 `apps`、`v1`、`deployments` |
| `namespace` | 精确命名空间，或明确以 `null` 选择凭据有权限的全部命名空间 |
| `label_selector`、`field_selector` | LIST/WATCH 共用的精确 selector，参与范围身份 |
| `credential_id` | 含 `token` 字段的 Secret Store 引用，配置中不放 token 真值 |
| `ca_pem` | 可选公开 CA PEM；省略时使用 SDK 实际 certifi CA bundle。冻结有效证书 DER 信任集合，并用同份已核验字节固定连接信任 |
| `mode` | 默认 `per_event`；必须显式选择 `state_sync` |
| `limits` | 只能收紧既有配额，不能绕过控制预留或共享上限 |

凭据需要对所选资源/范围具有 LIST 和 WATCH 权限。连接测试只验证有界 LIST/WATCH 访问，返回逻辑身份、范围指纹及 trust/list/watch 验证事实，不建 Batch、Receipt、checkpoint 或业务 Execution，也不证明完整源快照或目标对账成功。同引用 token 真值轮换在重连时读取；有效 CA 信任、origin、selector、资源范围或模式改变，须停用、真实关闭、责任结清并显式新 epoch。

`per_event` 保留逐条已接管变化。`state_sync` 仅在同 scope/对象 UID 的责任从未关联任何 Execution 时合并 pending WATCH 状态；即使旧执行之后又有 pending replay，也不能合并。LIST 成员和 `DELETED` 事件不合并。已有执行输入不改，首次/当前事件身份、合并次数和替代关联保留审计，合并不代表业务成功。两种模式都不能还原 Kubernetes 已丢弃的历史。带 selector 的 `DELETED` 可能仅表示离开所选范围，不等于对象物理消失；删除必须限定 UID 和 scope。

LIST 每页最多 16 个对象、原始体 1 MiB；WATCH 每行最多 512 KiB。成功和错误响应都在 JSON 解码前通过 `read1(4096, decode_content=False)` 有界读取，要求 identity 编码并禁重定向。对象 Envelope 仍不得超过 1 MiB 与 Execution 输入限额中的较小者。整批最多接管 5,000 对象、128 MiB 内容；超限或不完整快照丧失清理资格，保留已接管责任并走 failed 控制路径，绝不能据此推断未读取对象不存在。

每批预付三份各至多 8 KiB 的控制槽，覆盖订阅/全局的 pending、history 与 dedupe 计账，普通对象/WATCH 不得挤占。ready 控制仍服从同一每订阅10/s、全局50/s窗口并公平等待；预留不豁免 rate 或 Admission。空未闭合批次和剩余控制预留同样锁住运行变更。16 个原生连接名额和 64 MiB 应用缓冲与 Kafka、诊断共享，确认原生关闭才归还名额。raw/应用计账不是进程 RSS 硬上界；3 秒连接、2 秒 socket read、每次读取之间检查的 5 秒 deadline，不代表 DNS/OS 可被硬取消。

正常流程为 `sync.started` → LIST 成员 → `sync.completed`：所有页和成员最新责任成功后才能授予完成执行资格，Adapter 还须核对自身目标写入，才能清理。已验证 PodList/DeploymentList 的成员可省略 `kind`/`apiVersion`，原始 payload 保持不变；显式错误/null/空值、独立 WATCH 对象和顶层 LIST 身份仍严格校验。成员失败在快照证据仍有效时可修复；对成员或 started 显式 skip 则永久取消旧批清理资格。

部分 LIST 失败、权限失败或冻结前的历史缺口产生 `sync.failed`。failed 控制处理成功或经显式审计 skip 后，旧对象/WATCH 才可继续排空；这不授予清理资格，也不掩盖其他业务失败。完成资格在 T2 冻结，之后的新缺口记录为下一轮恢复，不把已冻结批次改写为 failed。成功闭合 completed 拒绝新 replay，再次对账使用新完整批次；未确认的失败完成链仍可受限 replay/repair 或使用前述专用放弃。

停用后应等待真实关闭；已接管责任和预付控制在功能关闭时仍接受有界维护。相同 epoch 再启用可用新 activation 从持久 opaque RV 续接；原批仍有效时不重新 LIST。BOOKMARK 仅在之前责任提交后推进位置，不创建业务 Execution。`410 Gone` 表示可读历史缺失：保留 gap、结清旧生命周期，再取得新快照，不能补齐漏掉的变化。正常 enabled 恢复可新建有效批次；专用放弃保持停用，旧责任结清后仍须显式启用。reset 仅在停止和结清后创建新 epoch并保留历史；Kubernetes 没有 Kafka 的 earliest/latest 或任意位置跳过动作。

| Kubernetes 诊断码 | 处置含义 |
| --- | --- |
| `source_permission_denied` | 核对 bearer token 引用及本资源/范围的 LIST/WATCH RBAC |
| `source_trust_invalid`、`source_tls_invalid`、`source_identity_changed` | 核对有效 CA、主机名和信任身份；不关闭 TLS 校验或静默沿旧 RV |
| `source_history_gap`、`source_list_snapshot_changed` | 保留已接管责任，检查批次 failed/冻结后的恢复路径 |
| `source_response_too_large`、`source_content_invalid`、`source_object_invalid` | 核对来源内容或范围；未接管数据尚未完成业务处理 |
| `subscription_sync_history_capacity`、`subscription_sync_batch_capacity` | 预留或整批容量不足；检查现有责任/留存，不放宽上限或删除受保护历史 |
| `subscription_sync_completion_conflict` | 完成资格或期望责任已改变，或旧控制已结清；新动作前先核对原请求结果 |

### B4 证据与尚待完成的 B5 门槛

源码 `326dca6` 已通过 B4 后端门槛：真实 Kubernetes 1.35 API、普通 Python Adapter/Worker、RabbitMQ Attempt/Outbox 关联和独立 PostgreSQL 目标读回。仅含元数据的 fixture 有 17 个选中 Pod，分页为16+1；LIST 成员加 started/completed 共19个 Receipt，一次 WATCH 更新后共20个。全部目标 payload/hash 和 UID 匹配，更新对象有2次有效写入，其余16个各1次。停用期间再次修改 annotation，同 epoch 重新启用产生新 activation，从持久 RV 接收 WATCH，仍只有原批次、无新 LIST、总21个 Receipt，选中对象3次写入；最后监听和应用缓冲归零。

最初 TypeMeta 拒绝保留为首败，随后通过窄修允许成员字段省略且不注入 payload；首败的 failed 控制业务成功后，失败批次闭合、预留归零并停用。故意请求旧 RV 只证明原生 `410` 诊断，不证明自然平台过期恢复。Deployment 真实目标路径、Kubernetes 浏览器操作及剩余 B5 代表恢复场景仍待验证；未声明 Kubernetes 稳态吞吐或长期容量结果。详情与未验范围见 [B4 审查记录](../../openspec/changes/add-event-subscriptions/b4-review.md)，精确 HEAD 远端 CI 与用户验收仍是独立门槛。

## B3 验证状态与容量边界

B3 已通过独立验收，Kubernetes 继续按 B4/B5 推进。匹配部署的 `10ec0f4` 已完成真实 Chrome 创建、无入组连接诊断、启停与实际关闭、Kafka 消息到目标、带同 partition 积压的受限 repair、skip、当前配置补处理和 reset。独立核对保留原失败终态，四条目标业务各有效写入一次，skip 无目标写入；reset 保留旧 Receipt/Delivery/checkpoint，Saved Input 不变且未重新启用。仅有一个 Adapter 读取权限的账号只看到授权订阅且无写入口，已打开事件详情可随轮询更新。普通 manual、实际 schedule、Webhook 三条真实 Worker 路径的 C14 兼容验证已通过。

浏览器另发现订阅锁定时普通 RunOnce 按钮及 Task 状态文案不一致；后端负测拒绝普通触发且未创建 Execution。`d48e3f8` 的 Web 窄修已通过组件、独立审查及匹配部署的双语 Chrome 窄复验：目录和 Header 显示订阅锁定，RunOnce 实际禁用，保存、Input 和 Worker 仍锁定；普通 manual Adapter 的运行按钮保持可用，未重复执行。新 epoch 的 latest 再次启用与停用未改变旧责任及目标唯一写入；运行版本另有逐哈希核对。

容量首轮使用同一个普通 Python Adapter、四个 partition、两个 Worker 执行槽及既有 1 秒 RabbitMQ 最小重投延迟：1,800 秒输入 1,800 条，持续阶段目标完成 1,732 条，约 0.962 条/秒，积压增长，因此持续 1 条/秒门槛不通过。停产后 1,800/1,800 完整收敛不改变首轮结论。仅按该次样本估算，Control 物理库增加约 14.81 kB/事件；持续 1 条/秒的线性投影不是已达到的吞吐或留存稳态。默认 10,000 条历史额度在该假设速率下约 2 小时 46 分 40 秒触限，不构成七天持续接收承诺。

既有 `DLR_RABBITMQ_RETRY_BASE_SECONDS=0.1` 已在停止应用、连接/消费者与全部消息计数连续清零的独占静默窗口下完成空开发队列维护；RabbitMQ 4.3.5 quorum 不支持条件删除，此操作不具备服务端原子空队列保护。产品默认值保持不变。

修订配置固定 `d48e3f8`，同一 Adapter、四个 partition、1 秒 tick、两个配置执行槽，预热另计后，正式约 1,800 秒发送的 1,800 条全部在阶段末写入目标。独立审查逐条核对完整 payload、来源位置和一次有效写入，积压未累计增长，30 分钟稳态门槛通过；发送至目标已提交行 insert 时间的 p95 为 1.98 秒。应用缓冲峰值 3,501 字节，Control RSS 尾十分钟仍有约 32 kB/min 小幅正斜率，不能据此声称无泄漏或任意时长稳定。此结果不外推默认 1 秒重投配置或七天持续运行。有限突发后已完成触限后的 600 秒观察与显式恢复，103 条输入明确分为 71 成功、2 显式跳过、30 因历史满额未接管；观察窗口 API 均满足两倍门槛，但短突发 runtime-status 的 10 个样本 p95 曾为基线 2.604 倍，保留该波动。已接管责任排空、监听关闭；历史满额仍等待合法治理，不加配额或提前删除原文。版本、首败、增长及未验范围见 [B3 审查与验收记录](../../openspec/changes/add-event-subscriptions/b3-review.md)。

## 迁移与应用回退

向前迁移 `0047_issue219_subscriptions` 增加订阅结构并扩展 trigger，`0048_issue219_kubernetes` 增加不可变来源身份和持久批次/控制/放弃计账；不自动创建订阅，也不改写既有 Adapter 输入。没有已验证身份的旧 Kubernetes 配置须显式新 epoch。首次部署先停止接收/写入、备份并验证可读，再运行迁移并更新 Control/Web；现有 protocol-v3 JSON Worker 无须增加输入类型。

回退旧应用前必须停用全部订阅，确认监听实际回收，处理或明确保全所有 pending、非终态 Execution 和失败屏障。开关关闭仍允许已经接管的责任排空。不能把“关闭功能开关”“容器健康”或“内部队列为空”单独当作责任已清空；应核对事件、执行和持久计数。保留新表和审计，不自动数据库 downgrade/restore。迁移在已有订阅历史时拒绝降级，避免删除已确认责任。

本地开发测试的入口、Compose 参数、凭据、备份和原始验证结果保存在私有配置/证据目录，不能提交到仓库。产品容量与实际来源支持范围以各来源验收记录为准。
