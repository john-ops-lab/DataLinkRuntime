# B0：共享运行契约与设计可行性审查

审查日期：2026-10-23。核对检出 `97b274e47d37e879871ecd144ba9a67dab5675e8`；该提交只增加规划和规则，运行源码基线为 `eedda36f9ed1e3ab4f84b0a76532d3916f8ca6d5`。本记录对应任务 1.1、1.5，只做源码和设计核对，没有实现 B1、运行连接器或触碰容器。

结论：T1/T2、冻结 activation、带积压修复和批次失败退出能够在现有可靠执行底座上实现。未发现需要更换架构或削减产品合同的源码阻塞。下列兼容改动必须随公共底座完成，不能把当前代码直接当作已经满足合同。本记录不能单独证明 B0 的客户端、资源预算或真实来源门槛通过。

## 必须同批处理的兼容面

以下路径相对仓库根，行号对应上述源码基线。

| 契约 | 当前证据 | B1 必须完成的改动或验证 |
| --- | --- | --- |
| `subscription` trigger | `backend/src/dlr/control/models/execution.py:143` 只允许 manual/schedule/webhook；字段长度 16 足够保存 subscription | 同时升级 Alembic 约束和 ORM；已有三类数据不重写，schedule 唯一点约束不扩展到 subscription |
| API 历史筛选 | `backend/src/dlr/control/api/executions.py:200` 是三值 Literal；`services/execution.py:519` 的查询按 trigger 在分页前筛选 | Literal 增加 subscription；验证权限与分页不变 |
| API 序列化 | `backend/src/dlr/control/schemas/execution.py` 的 Response/Summary trigger 是 str；input_snapshot 是 dict | trigger 本身可序列化；新增来源追踪通过独立关联表达，不能把 Receipt/Batch 操作能力塞入普通历史读取 |
| Web 历史 | `web/src/api.ts:542` 限制三类筛选；`components/ExecutionHistoryPanel.tsx:51` 只有三种翻译并以未知值兜底；`types.ts:181` trigger 为 string | 同批增加类型、历史标签和 zh-CN/en；完整配置/追踪交互仍在 B3/B5 交付，默认关闭的 B1 不显示来源可用 |
| JSON Snapshot | `models/execution.py:332` 保持 json 类型可行；`schemas/reliable_runtime.py:229` 已支持 json 且 snapshot 为 dict | 内部可信事件入口直接构造 Envelope 和 `{source_type: json, revision: null}`，不调用 Task 当前输入 resolver，不开启普通 per-run override |
| 输入 revision 列 | `models/execution.py:333` 为非空整数；实际迁移 `backend/alembic/versions/0027_issue127_a1_execution_input.py:50` 另有 `input_config_revision > 0` 检查；现有 `services/webhook.py:450` 使用兼容值 1，未读取 Task InputConfig | 内部列可继续使用明确命名的兼容 sentinel=1，公开 input_snapshot.revision=null；两者不是同一字段。无需仅因 snapshot 的 null 改数据库列。不得把 sentinel 描述为 Task 输入版本，或把 subscription config_version 填入本列；若详情 API 对外仍带 input_config_revision，集中序列化需明确其对 subscription 不适用（可只对公开层映射 null 并同步 schema/Web，数据库仍为正数）。Worker payload 不包含该列 |
| T2 事务 | `services/reliable_execution.py:211` 的 accept_execution 只 flush/建 Outbox，不 commit；`services/execution.py:325` 的上层 create_execution 会 commit | 使用前者，在同一 Session 和外层事务内创建 Execution、Delivery 关联及 pending 释放。失败必须整笔 rollback；不能捕获错误后提交计数或部分 Execution |
| 原 Revision/Worker | accept_execution 支持 version_id 和 worker_id_override；`services/execution.py:161` 使用版本 override，`services/worker.py:408` 按 execution.version_id 读代码 | T2 显式提供 activation 版本和 Worker，先校验属于本 Adapter；不能恢复时自动读取 latest。原 Worker 不可用时保留 pending 并报告原因 |
| 原凭据绑定引用 | `services/reliable_execution.py:285` 总是查询当前 Adapter bindings；`services/execution.py:87` 可保存闭合引用集；`services/secrets.py:394` 从 Execution 的引用行在 Claim 时解密当前真值 | 给可信入口增加显式、严格校验的 activation 引用参数；activation/Receipt 在 T2 前也要保护 credential 外键，不能等 Execution 创建后才保护。绑定 ID 只是来源事实，凭据真值仍不快照 |
| 单项大小和 Admission | `services/admission.py:78` 对 json 计算 JCS 字节；accept_execution 本身不运行 Task resolver 的 execution_input_max_bytes 检查 | T1 验证原始来源和最终 Envelope 大小及 JSON/JCS 域；T2 仍调用 Admission 和 Outbox 容量检查，不能以已经入 Inbox 为由绕过 |
| 运行锁详情与列表 | `services/adapter_runtime.py:33`、`:50` 只认识 running 和启用 Schedule/Webhook | 两条有界查询路径都加入 enabled/starting/stopping、pending、非终态关联执行及 repair；返回一致锁原因，避免每个 Adapter 单独查订阅 |
| 当前输入写入 | `services/input_config.py:450` 有独立 `_require_user_runtime_unlocked`，没有统一委托中心 predicate | 必须同步此路径，不能只改 adapter_runtime。STAGED 上传治理仍可使用，绑定当前输入必须被锁拒绝 |
| 普通保存与绑定 | `services/adapter.py:280`、`:615` 及 `services/secrets.py:311` 在 Adapter 锁下校验运行锁 | 订阅启用/排空时普通版本、Worker、timeout、输入及绑定保存拒绝；元数据修改仍可用 |
| 手动与其他触发入口 | `services/execution.py:325` 的手动入口当前不检查订阅；Schedule/Webhook 更新各有启用逻辑 | 手动执行、Schedule 启用、run_mode 切换均在 Adapter 锁下验证订阅互斥。检查所有入口，不能只依赖 UI 或一个运行锁函数 |
| 删除和克隆 | `services/adapter.py:488` 的 stop/delete 可取消 queued 并永久删除；`:673` 克隆复制当前配置 | 存在未完订阅责任时，即使 stop=true 也不得复用旧删除语义抹去责任。克隆只复制声明配置、默认停用，不复制 lease/checkpoint/activation/Receipt/Batch/group 占用 |
| 通用 replay | `services/attempt.py:1451` 对 JSON dead_letter 直接允许 replay；`:1483` 的 replay 会 commit，默认选当前 Worker/绑定 | 通用 replay 可用性和执行路径都明确拒绝 subscription 并引导独立订阅动作；不能把它作为订阅 replay 的实现。订阅 replay 要有 request_id、原因、原引用及来源屏障关联 |
| Execution 留存 | `services/retention.py:41` 只有三类策略；`:67` 与 `:107` 仅保护 Idempotency、pending Outbox | 增加 subscription 策略及订阅保护引用，选择和实际删除都校验。否则会永不清理新 trigger，或在未来扩展策略时误删阻塞/批次证据。清除原 Execution 后保留不可操作历史身份和“原执行已清理”事实 |

必须联动升级：PostgreSQL 向前迁移、Control（模型/服务/API/后台任务）、Web（类型、历史标签及后续批次界面）。现有 protocol v3 Worker 对 JSON Envelope 无新增字段需求；不能仅因新 trigger 要求所有语言 Worker 协议升级。旧 Control 不处理新责任表，不承诺在有未完责任时裸回退旧镜像。

## Worker 与多语言输入证据

- `backend/src/dlr/control/services/attempt.py:314` 将 input、input_source_type、input_snapshot 传入 V3TaskPayload，不向 Worker 传 trigger；`schemas/reliable_runtime.py:215` 保留五种语言和 `input: Any`。
- `backend/src/dlr/worker/executor.py:937` 校验运行外层，`:1591` 把完整 input 写入 `input.json`。事件只是 JSON 对象，不占用 input_files 入口，也不改变文件 manifest。
- `web/src/components/ExecutionInputSummary.tsx:16` 的快照解析目前只接受数字 revision；JSON 显示会退回 input_source_type，通常不报错，但 B1 应显式允许订阅 snapshot 的 null，避免仅靠容错显示。`web/src/types.ts` 的快照类型同步表达这个事件分支。
- Python：`backend/src/dlr/runtime/harness.py:246` 用 json.loads 读取；JavaScript/TypeScript：`runtime/node_harness.py:125` 用 JSON.parse；Java：`runtime/java_runtime.py:72` 用 Json.parse；Go：`runtime/go_runtime.py:122` 用 readJSON/unmarshal。
- Kafka partition、offset、timestamp_ms 的协议类型必须由 Envelope schema 固定，尤其 offset 为十进制字符串，防止 JS 数字精度改变。K8s RV 保持字符串；JCS 拒绝超出数值域的对象，按设计拒绝接管，不能静默改写。base64 保留 Kafka NUL、二进制和重复 header。
- B1 验证应覆盖同一 v1 Envelope 的公共序列化/解析及既有协议回归；真实业务运行用一个 Python Adapter。除非改变语言 harness，不扩成五语言 × 两来源故障矩阵。

## T1/T2 与锁顺序

设计的顺序能够嵌入现有 `Adapter → AdapterAdmission → GlobalAdmission → Execution/Outbox`，证据是 `services/admission.py:40`、`:116` 和 `:242`。新代码取得 Adapter 后再插入订阅相关锁；既有路径只取子集。T2 进入 accept_execution 前可持有订阅和 Inbox 计数锁，后续 Admission 仍按既有顺序。

必须保持以下实现条件：

1. 重投查询在新接管额度和 Rabbit readiness 前完成；不能把 accept_execution 的早期 readiness 检查当作 T1 去重入口。
2. T1 不占用 Admission/Execution Slot；数据库事务外做来源 RPC。Receipt 唯一约束按 subscription/epoch/source_event_id，T2 的 Delivery 关联另有数据库唯一防线。
3. 结果报告/终态更新不在持有 Execution 锁后回头加 Subscription 锁。用下一次独立幂等观察事务推进屏障；延迟只影响吞吐，不改变来源顺序。
4. 续租只锁 Lease，不能随后反向取 Adapter；变更 desired/generation 的事务始终从 Adapter 开始。批量处理按 Adapter、shard、Receipt/Batch ID 排序。
5. 现有 `_create_pending_execution_locked` 在创建 Execution 前经过 WorkerCacheGuard（`services/execution.py:168`、`services/cache_governance.py:75`）。订阅需沿用这个保护；不能持有 cache/Execution 锁后倒取订阅锁。若把 activation 计入缓存保护，查询须有界且不能引入反向行锁。
6. 配额核对先只读计算候选，修复时再按同一锁序重新计算/应用差额。不能拿着全局计数器遍历反锁各订阅；多 Adapter 页需先按 ID 取齐 Adapter 锁。
7. Execution 清理选择和删除两次都判断引用。不要只依靠先查询后无条件 DELETE；数据库 FK/条件保护要兜住 concurrent replay、batch 和屏障建立。

## source identity 与 epoch 可行性

源身份与内容哈希独立于现有 HTTP Idempotency-Key；`backend/src/dlr/common/jcs.py:37` 现有 payload_hash 包含 trigger/body，不能直接对含 received_at 的整个 Envelope 重用。

- Kafka：固定逻辑 cluster identity、topic incarnation 或明确配置的 incarnation/epoch 边界、partition、offset；稳定内容包含原始 key/value/有序 headers 和源不可变属性。首次 earliest/latest 和新增 partition 起点必须持久化；安全 checkpoint 按实际已读顺序推进，不能用数字连续性推断 offset 空洞。
- Kafka 无法经选定客户端验证 topic 重建时，必须在支持矩阵明确限制并要求显式 epoch；不能把“同名 topic + offset”视为永远同源。自动身份检测能力是否足够由客户端探针记录提供，不能由本源码审查替代。
- Kubernetes：逻辑 cluster identity 与连接信任共同确认；对象身份包括 GVR、namespace、UID，WATCH 再加类型/RV，LIST 再加 batch ID，生命周期为 batch ID + kind。RV 不排序；本地序号承担派发顺序。
- 凭据真值轮换、名称和描述变化不换 epoch；换来源、范围或重新选择起点明确新 epoch，并先处理旧责任。无法比较集群真身份的场景拒绝声称自动防止误连，由配置确认和信任边界承担。

上述元组和规范编码需在 B1 固定测试向量，属于 v1 协议实现，不可把字段顺序/数值类型留给各连接器自由选择。

## 受限修复 Revision 可行性

AdapterVersion 已是不可变行，且 `services/adapter.py:623` 通过 Adapter 行锁串行分配 seq；现有 save_version 同时 commit 和推进 latest，不能直接嵌入 repair 多步事务。

B1 独立 repair 动作需在同一 Adapter 锁下确认：desired disabled、listener 已回收、repair 停止新派发、无非终态旧 Execution（因此无 active Attempt）。保留 pending 可以；只保存代码/依赖，runtime_config、Worker、输入、来源和凭据引用必须保持原值。可提取不 commit 的版本保存原语，但不能让普通 save_version 绕过运行锁。

修复版本只被明确选择的 replay Delivery 使用；旧 activation 和其他 pending 保持原 version_id。成功退出 repair 时，只解除对应失败链/屏障；后续旧版本再失败仍逐项处理。普通 Execution Replay 不能进入这个流程。旧 Revision、Worker、credential 的缺失分别返回明确原因，不自动替换。

## 失败批次退出与生命周期额度

设计 D7/D9 的退出路径可行，前提是把 Batch 资格和控制预留作为持久原子事实，而不是在内存计数。

- 开批在订阅/全局 pending、原文历史和去重元数据各层同时预留 3 条、每条至多 8 KiB 控制额度。普通对象用 `used + reserved` 进行上限判断；只减少 pending 不能释放历史/元数据预留。
- started 成功后派发成员。partial LIST、410、skip 成员、整批超限或历史预算不足把未冻结批次变 failed；预留额度保证 failed 可落库。failed 可以越过批次等待但不能越过 active Attempt；failed 自身失败须 repair/显式处置，不能自动视为成功。
- failed 被处理/处置后放开旧 LIST/WATCH 排空，普通业务失败屏障仍有效。批次永不重新取得完成资格；已写目标不回滚为不存在。关批释放未用预留，并解除已终态成员的批次保护，仍遵守 7/30 天窗口。
- completed 的 T2 与 failed/superseded 共锁 Batch；先提交者决定资格。completion_committed 后 WATCH 新 gap 进入下一轮恢复，不能以 failed 撤销已创建 completed，避免外部目标副作用与内存状态矛盾。
- 控制记录 reserve 不绕过现有 Execution Admission。Admission 暂满时生命周期记录仍在持久 Inbox，待已存在 Execution 收敛后进入 T2；不能因此丢掉控制责任。
- 超过整批或历史预算必须失败并关闭本批，不能等待被本批保护的数据自行过期。保留窗口阻止下一批时保持容量阻塞，不能热循环开新批。

## 审查分类与后续门槛

**需要在 B1 明确的共享表示选择**：事件没有 Task InputConfig revision，但旧数据库列强制正整数。沿用 webhook 已有的内部兼容值 1 即可满足存储约束，公开 snapshot 保持 null，并把该兼容值明确为非权威内部字段。不存在必须扩大数据库迁移的阻塞；公开详情字段、SSE 和 Web 必须一致表达“不适用”，不得以错误 Task revision 展示给用户。该字段的集中序列化实现属于 B1 兼容工作。

**B1 正常实现工作，不是架构阻塞**：新表/唯一约束、activation 凭据引用参数与保护、中心及旁路运行锁、通用 replay 订阅守卫、清理保护、事务唯一性、控制预留和 Batch 冻结；这些均已在原任务 2.1–2.6 的范围内。

**本审查不覆盖、不能标为已验**：客户端精确版本/许可/wheel、真实 Kafka 身份/commit/rebalance 行为、K8s 分页/WATCH/关闭连接探针、专用线程池取消与 HTTP/RSS、磁盘增长测量。对应其他 B0 记录全部通过并完成独立批次审查后，才可进入 B1。B1/B4 必须用确定性事务交错测试证明修复、配额和失败批次没有退出死锁，不能仅凭这份可行性说明通过实现验收。
