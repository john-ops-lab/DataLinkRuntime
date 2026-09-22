## Context

动机见 `proposal.md`。基线已有 `VerifiedVersionCache` 的精确 manifest、只读内容、跨进程容量预留、tmpfs staging 与原子发布；`venv._lock_for` 仅是安装期间的线程锁，返回 runtime 路径后没有覆盖进程使用。`remove_entry`、旧 stale/Adapter 清理以及坏环境重建目前均能直接删除，必须统一收口。

唯一执行链路是 RabbitMQ v3，Control 已保存不可变 version、目标 Worker 快照、Attempt、Incident 与恢复责任；Worker 有 Attempt/Workspace/Sandbox 私有 journal。不能把一次引用快照或过期的 reservation 当作删除授权。现有 ready manifest 字段精确匹配，向其中增加最近使用字段会破坏旧缓存兼容。

## Goals / Non-Goals

**Goals:** 冻结足够小、可证明的新引用/删除协议；A 先验证安全与升级兼容，B 在同组增加治理；默认不自动删除，确实可重建且未被保护才有候选。

**Non-Goals:** 不修改当前执行状态机、ACK/Lease/Fencing 语义，不引入 Worker 数据库连接、新消息基础设施、通用恢复框架、依赖锁文件自动生成器或共享包缓存清扫器；不以本变更重做已完成组2。

## Decisions

### 1. Identity and independent lifecycle metadata

- 版本缓存身份继续使用当前 `{adapter_id, version_id, language, source_sha256}` 与内容 manifest digest；当前五语言各自 source 输入保持不变，不趁升级重算 identity。Worker ID 和本机 cache root 归属属于外围事实。
- 在 `version-cache` 的私有 sibling 元数据目录保存版本化生命周期、使用记录、锁与删除记录，权限目录 `0700`/文件 `0600`，采用现有 fsync + 原子 rename。它们不能进入 `entries/<key>` 内容树或原 manifest；接口不返回真实路径。
- root 持久保存 schema、随机 store ID 与所属 Worker ID。注册因改名获得不同 ID 时不得覆盖 owner 或按新 ID 清理旧内容，报告 `cache_owner_mismatch` 并停用治理。全新空 root 可自动绑定；非空旧 root 只有从升级前注册信息/部署事实明确确认原 Worker ID 后才可初始化，不能仅相信此次注册返回 ID。无法确认则旧 ready 仍可验证使用、治理保留；实施与部署验证必须保存原 Worker ID 绑定证据，不引入自动跨 Worker root 接管。
- 每项保存 key、已验证 identity/digest、实际字节、`first_observed_at`、`last_used_at`、pin、rebuildability、策略 revision；使用时间取成功获得使用锁并验证/准备环境的时刻。pin/rebuildability 更新也取同一项锁。信息冲突、损坏或写入失败返回稳定保留原因，不能清空重建元数据替代检查。
- 缺少生命周期文件时先保持旧 `verify()` 与命中路径。有界扫描通过内容校验后以“现在”初始化观察/使用下限、`rebuildability=unknown`；不从 mtime 推断闲置、不把缺失值解释为 0。不修改内容字节/权限/manifest，不需要全量预迁移；中断可重入。仅生命周期 sidecar 不可写时仍允许验证通过的旧环境执行，但该环境不可回收；cache-use 责任记录写入失败必须在任何依赖/进程副作用前 fail closed。

选择独立 sidecar 而非扩展 manifest，是为了保持旧 ready 精确校验与 digest 不变。内容被篡改仍必须失败，禁止放宽字段或跳过 hash。

### 2. Local use covers preparation through process exit

- 每个版本 key 使用稳定的跨进程排他 `flock` 文件，文件在条目树外且不随条目删除；配套线程互斥/重入管理，避免同进程多线程和嵌套 prepare 死锁。不同版本互不阻塞；同一 Adapter 原本只有一个 active Attempt，独占锁不降低当前有效业务并发。同 root 升级必须先停止旧 Worker 并确认其进程已退出；不支持不参与新锁的旧 Worker 与新 Worker 同 root 混跑。不同 root/节点的旧 Worker 兼容不受此限制。
- executor 在任何依赖查找/验证/安装前持锁，在已有 Attempt journal 后、使用环境前持久化独立 cache-use 记录，关联 key、Execution/Attempt/fence。锁覆盖准备、ready 命中、加载、整个子进程、取消和 Sandbox 确认清空。只有证明子进程/cgroup 空且不再读取缓存，才删除 use 记录并释放锁。
- 崩溃会释放内核锁但不能释放持久 use/journal 保护。启动恢复先完成现有 Sandbox 恢复；未解释的 use、Attempt、Workspace 或 Sandbox journal 一律保留相关项。旧 journal 本身没有 version，必须按 Execution/Attempt 向 Control 解析；无法映射版本、PID 重用或 Sandbox 是否为空不明时，保守暂停整个版本缓存删除。现有流程可能在 cleanup deferred 时移除 Attempt journal，因此不得仅查 Attempt journal；`dependency_check` 早退、ownership lost 与异常路径均须保留直到 Sandbox 确认清空。清理回执未确认仍需结合 Control 责任复核。
- 对非 executor 的 prepare helper 保留明确的使用上下文；可重入 prepare 不能创建绕开锁的公开路径。容量预留继续只代表安装字节，不代表运行保护。

### 3. Durable Control guard serializes future references

- 新增 `worker_cache_guards`，唯一键 `(worker_id, version_id)`，包含 adapter ID、单调 generation、可空 operation ID、阶段。它是删除后的稳定锁锚点，不受 Adapter/Version 删除级联影响；不用 TTL 自动释放。只存非敏感 ID。
- 统一锁序为已有 `Adapter → AdapterAdmission → GlobalAdmission`（涉及这些锁时）→ 按 key 排序的 cache guard → Execution/Attempt/Incident。新引用入口与 GC 必须采用同一序，不在已经锁 Execution 后反向取 guard。新建 `_create_pending_execution_locked`、Replay、重试/恢复重新激活、Incident 人工恢复和 claim 都必须覆盖。新 Execution 必须在插入/commit 前检查目标 Worker/version guard；执行中的既有引用先存在就阻止 GC。
- GC 对仍存在的 Adapter 先锁 Adapter，再 upsert/锁 guard 并在同一事务复核引用；已删除 Adapter 使用独立 guard 后复核，不能因为缺少 Adapter 就视为无引用。`target_worker_id_snapshot`、`target_worker_id`、实际 Worker 与 Attempt Worker 均是责任来源；变更 Adapter 当前节点不释放旧节点责任。
- 保护集合至少包含 `queued/running/retry_wait`、非终态 Attempt、未完成 Sandbox/Workspace cleanup、open Incident 及其可恢复材料。`dead_letter` 若有 open Incident/清理责任仍保留；只剩普通终态历史不永久保护缓存。查询不完整、数量截断、历史必需字段缺失或责任矛盾均为 unknown，不发授权。
- 同事务确认无引用且 Worker 具备 `cache_governance_v1` 后占 guard，提交唯一 operation ID/generation。后来的新引用返回 `409 cache_reclamation_in_progress` 与短暂重试提示；不得创建半个 Execution、消耗 Admission、推进 Schedule 游标或 ACK 掉可恢复工作。现有入口按原失败合同回滚；claim 返回其既有可重试暂停语义。
- 引用先提交则 GC 保留；GC 先提交则新引用暂缓。不能用“扫描后又查一次”代替这一门禁。仅 Worker 同 key/generation 的完成或安全撤销回执能释放 guard；Control 断联、超时、重启和旧操作回执均不释放。

独立持久 guard 加入现有事务，比跨 HTTP 持有数据库事务或新的分布式租约系统更小；无 TTL 消除了 Worker 暂停后持旧授权删除新引用的漏洞。

### 4. Deletion primitive and bounded recovery

1. 预览只产生候选及理由。执行每项前获取本地项锁，验证 root、直接子目录、uid、类型、无叶子 symlink、identity/digest、生命周期、pin、可重建、最近使用及本地 journal。
2. 在持锁期间向 Control 申请 guard；再次核对返回的 Worker/key/operation/generation，并持久写入本地删除记录，之后才允许破坏性操作。任何一步失败保留原条目。
3. 原子 rename 到受控私有 trash，以记录绑定旧 identity 与来源，fsync；不先修改仍在 `entries` 中的只读内容。按单轮文件/时间/字节预算逐步删除 trash，残留继续计入实际占用。
4. 完整删除后持久记录结果，向 Control 提交同一 operation 幂等回执，收到确认再完成本地记录。回执仅更新本次缓存状态/审计，不能改变 Execution。
5. 启动和周期恢复通过有界 API 枚举本 Worker 未终结 guard，与本地记录取并集，覆盖“Control commit 成功但 acquire 响应丢失/尚未写本地记录就崩溃”。在同一项锁下，删除记录存在则核对 guard 和受控 trash，只推进该对象；没有本地记录/没有发生 rename 且原内容完整、无对应 trash 时可安全撤销，不能删除它后再报撤销；内容/记录矛盾保持 `cache_cleanup_unknown`。旧命令每次开始/恢复破坏性操作前必须核对当前 guard，完成/撤销的 operation 不可重放。未返回响应的 acquire 也用客户端生成的 operation ID 幂等重查。

无自动无限重试：记录尝试次数、下次重试时刻，默认 60 秒退避，连续 3 次失败进入明确失败/保留状态，管理员可发起重试；时间预算耗尽的正常分批推进不是失败次数。处理中断的 guard 保留，新运行收到可解释暂缓，不能由管理员绕过安全条件强删或凭超时解除。

占用由实际目录扫描与预留状态核对，保留 ready、trash 和失活 staging；活跃 staging 由 reservation charge 覆盖，不重复计费。只有文件已实际消失才报告释放字节，不采用可重复扣减的累计计数。清理自身不得写入 Adapter/Version/输入/历史对象。

所有旧清理入口均调用治理服务或返回 retained，不能直接 `rmtree`：`cleanup_stale_venvs`、`cleanup_adapter_environment`（包括 pre-cache 目录）、`agent._execute_cleanup_task`。部分保留/失败不能假报整个 Adapter cleanup 成功。未知 pre-cache 树只能报告。

坏缓存修复与淘汰分开：真实 prepare 可修复已证明损坏或不可用的同 identity 环境，也须保留 builtin 材料、TypeScript/Go 工具链或既有选源配置变化后的合法 identity replacement。旧对象的 adapter/version/language 归属须由可解析旧 manifest 或既有已验证 sidecar 绑定，不得只凭目录名推断；两者都损坏/缺失或归属冲突则保留。版本环境中的合法 venv 内部软链保持原校验语义，只禁止删除根/边界逃逸软链。

采用绑定当前 Attempt 的 `replacement` guard，共用同 key 使用锁、持久责任、路径和中断恢复原语；这是正在执行的依赖准备例外，不能用于空间回收、管理员清理或有效同 identity ready 的“重建优化”。Control 按第3节锁序，在同一事务验证以下全部条件：

- 当前 Attempt 已 `running`，Worker、Execution/version、claim token、fencing token 与业务 slot 的 active Attempt 一致，租约有效且未请求取消；Worker 已持同 key 排他锁和本次 use 记录。guard 绑定该 Attempt/fence 与旧、目标 identity，不能被另一 Attempt 继承。
- 除当前 Attempt 外，不存在同对象的 `claimed/running` Attempt、未确认的历史 Sandbox/Workspace 清理、open Incident 或恢复责任。Worker 对其他 use/Attempt/Workspace/Sandbox journal 同样保留；只能精确排除本次 Execution+Attempt+fence 的记录，不能按 Execution 整体忽略旧 Attempt。发现未知、矛盾或截断结果则拒绝。
- 其他未完成引用只允许尚未领取的 `queued` 或等待下次 claim 的干净 `retry_wait`，且引用同一不可变 version、`builtin_package_snapshot` 与当前 Execution 完全一致（双方为 null 也算一致）。已有历史 Attempt 时逐项证明终态且清理完成；没有任何 Attempt 的新 queued 其初始 `workspace_cleanup_status=pending` 不代表旧清理责任。不同 builtin snapshot 或不能证明未准备的引用保护旧对象。

这些未来 claim 的引用没有冻结旧源 URL 或旧工具链身份：现有 `claim_dispatch` 在 Adapter 业务 slot 中串行领取，存在其他 active Attempt（包括过期待恢复）时会暂缓；`build_task_payload` 在 claim 时解析默认源，执行器此后才应用 Worker fallback 和本地工具链。replacement 复用当前有效 payload 和现有 identity builder，不向排队任务承诺它们尚未取得的旧 URL，也不声称其他引用永远计算同一目标 identity。后续 claim 若选源、凭据或工具链变化，继续按原 prepare 合同命中或受控重建。因此不增加 PackageSource revision、源/凭据全局锁、配置快照或解析输入指纹 API，也不向治理接口输出解密 URL、凭据或代码。普通 GC 仍被任何 queued/retry_wait 引用阻止，不能复用 replacement 例外。

旧实例在新 staging 完成准备和完整性验证之前保持保留；切换使用既有受控 trash/持久记录，直到新 ready 验证发布后才完成旧实例删除及 guard 回执。准备失败时不把旧实例当已释放空间；恢复只按记录核对已知旧/新 identity 后继续或安全恢复旧实例，账目同时包含仍存在的旧、新内容。当前 Attempt 失去有效所有权后不得继续开始破坏性步骤，未完操作由原 generation 的恢复协议收敛。

**A2 replacement 条件已冻结；实现通过针对性测试和独立 Review 后才能完成 A。** 最小回归包括：多个同版本、相同 builtin snapshot 的 queued/retry_wait 不互相阻止合法重建；后续 claim 选源变化仍按原合同准备；不同 builtin snapshot、其他 claimed payload、历史 deferred cleanup、旧 journal/open Incident 均保护旧实例；准备失败/所有权丢失/切换中断不丢旧实例、不重放旧授权。保持 tasks 1.1–1.3 的本地基础接口不变。

### 4.1 Legacy cleanup identity, claiming and aggregate completion

旧 Adapter cleanup 复用 `WorkerCleanupRequest`，不得被它自己的未完成哨兵永久阻塞。治理 Worker 的 claim 回显已有递增 `attempts` 为 `claim_attempt`；acquire 可带 `cleanup_context={cleanup_id,claim_attempt}`。Control 按 guard → cleanup 锁序核实同 Worker/Adapter、当前 running 领取且 Adapter 已永久删除后，仅从引用查询排除这一条 cleanup；其他 cleanup、Execution、Attempt、Incident、hold 和全部本地 use/journal 保护不变。普通 GC 没有此例外。结果回报绑定当前 claim_attempt；旧领取不能完成新领取，同一成功领取重复回报幂等。旧 Worker 保留原协议，新能力 Worker 必须提供领取序号。

首次升级可能没有 guard，且 Adapter/Version 已硬删除。Worker 在同项锁下验证 root owner、实际位置、完整旧 manifest/content 及无冲突 sidecar 后，可在 acquire 提供 `observed_identity={store_id,language,source_sha256,digest}`。Control 核实现存 Version 的真实 Adapter 关系；两者均不存在且无 guard 时可建立 idle/generation=0 稳定锚点，再依原规则复核引用。不得复活业务行或将业务行缺失当成无引用。已有 guard 的 adapter 不可改；身份/store/digest 与 cleanup_context 绑定本次 operation 并在幂等请求核对，不成为禁止合法 replacement 的永久 guard 身份。该声明属于现有可信 Worker 管理域，不是远程文件验证。所有重建、pin、近期保留及离线条件照常生效。

仅 `.ready` 的 pre-cache 不具备受支持的实际对象身份绑定，本轮 retained；不得借目录名、其他路径同 key sidecar 或重新 hash 自行认领。entries 为空但旧 pre-cache 非空也不构成全新空 root。无法解析已删除 Execution 的旧 journal 继续 block_all。

旧 stale/Adapter helper 返回有界结构化聚合结果；只有完整续游标扫描结束、所有目标确实删除或已确认不存在、无 retained/failed/trash/未确认 guard 回执，才可成功。未知目标归属不能算扫描完成；共享缓存与已证明其他 Adapter 对象不属于目标。预算耗尽保持本次 running 并续作，不耗费失败重试次数；真实保留/失败沿初始最多三次领取机制停止，不能用最后一次 rmtree 收尾。控制器在持 cleanup 锁时仅以不加行锁 EXISTS 检查活动子操作，禁止 cleanup → guard 反向锁序。

重注册后的 claim_attempt 可以递增，但已取得的 operation 保留原 generation、identity 和原领取 provenance。恢复先沿原 operation/check/list 推进；新领取仅负责最终聚合回报，不借旧领取首次授权新对象，不清 guard 或改变旧 operation 绑定。

### 5. Rebuildability and cache classes

| 类别 | 身份与保护 | 本轮支持 |
| --- | --- | --- |
| 版本环境 | 原 manifest、key、Control 引用、use/journal、pin | 安全预览/手动/周期/容量回收 |
| 共享下载缓存 | 明确识别的 uv/npm/Maven 等 Worker 共享根；与 Adapter 私有树分开 | 有界占用报告，`shared_cache_not_supported`；不伪报可回收，不整体删除 |
| 失败安装 staging | 精确受控 staging 名称、归属记录、对应 reservation 和同项锁 | reservation 失活且无安装进程/use/journal、超过 TTL 时安全清理；未知 staging 保留 |
| Attempt tmpfs | 现有 Sandbox/Workspace 所有权 | 沿用已有清理，不计为长期版本缓存 |
| pre-cache/未知树 | 无法验证当前身份 | 仅报告 `cache_ownership_unknown` |

版本环境默认 `rebuildability=unknown`，即使依赖声明包含固定版本也不自动认定可重复，传递依赖和包源可变。管理员可对明确 identity/digest 设置 `confirmed`，输入固定为非敏感 `evidence_note`、`source_policy=verified_offline|managed_online`、`valid_until`（不超过确认后 24 小时）；记录操作者与确认时间。两种 source_policy 分别表示管理员确认“有当前可用、不可变且覆盖该环境全部依赖的本地材料”或“有受控、可重复且当前可用的在线源”。这是一条明确、限时的运维承诺，不是平台自动验证了外部仓库；UI 必须完整显示确认语义，不能只写“允许清理”。内容 identity/digest 改变或确认过期即失效，pin 优先。

离线保护默认启用：Control 不可达、明确部署 `OFFLINE_MODE=true` 且仅有 managed_online 确认、确认缺失/过期或最近依赖准备已对同来源报告不可用时保留。有效 verified_offline 确认不依赖网络；有效 managed_online 确认是明确可信输入，不再因为没有额外健康服务而永久 unknown。源不可用事件只从已有依赖准备结果提取稳定失败事实，不新增主动探测或通用源健康服务；管理员重查材料后重新确认才解除该失败保护。无外部依赖且平台内置材料 identity 可验证的环境可由系统自动确认。仅名称固定、URL 可达或历史安装成功均不足以自动确认。关闭 OFFLINE_PROTECTION 仅是部署策略，不是前端按钮，且不替代有效 confirmed。

### 6. Policy defaults and budgets

策略由 Worker 环境变量读取并启动校验，Control/Web 展示每个 Worker 的实际生效值；本轮不增加跨节点动态策略系统。所有数字必须有限、类型正确，配置不合法启动拒绝，不静默转为无限制。默认值是保守产品初值，生产启用须完成 A 安全门禁并显式配置。

| 配置（前缀 `DLR_CACHE_`） | 默认 | 合同 |
| --- | --- | --- |
| `GC_ENABLED` / `PRESSURE_GC_ENABLED` | `false` / `false` | 分别控制周期/容量自动删除；只读统计、预览和经安全协议的手动操作可用 |
| `SCAN_INTERVAL_SECONDS` | `300` | 10–86400；单 Worker 每轮互斥 |
| `IDLE_TTL_SECONDS` | `2592000`（30 天） | 60–31536000，周期候选闲置下限 |
| `MIN_IDLE_SECONDS` | `86400`（1 天） | 0–IDLE_TTL，容量/手動均保留近期版本，测试环境可显式为 0 |
| `MAX_BYTES` | `4294967296`（4 GiB） | 复用现有总预算语义，正整数 |
| `HIGH_WATERMARK_PERCENT` / `LOW_WATERMARK_PERCENT` | `85` / `70` | `0 < low < high < 100`，容量回收到 low 或满足此次 reservation 所需即停 |
| `DISK_RESERVE_BYTES` | `134217728`（128 MiB） | 复用现有磁盘低余量，非负且小于 MAX_BYTES |
| `MAX_DELETE_BYTES_PER_ROUND` | `268435456`（256 MiB） | 正整数 ≤MAX_BYTES；不开始超过剩余额度的整项，明确 `cache_budget_exhausted` |
| `MAX_DELETE_ENTRIES_PER_ROUND` | `20` | 1–1000 |
| `MAX_SCAN_ENTRIES_PER_ROUND` | `200` | 1–10000；稳定游标，截断项不默认安全 |
| `MAX_SCAN_NODES_PER_ROUND` | `100000` | 1–1000000；单项递归也消耗此预算，超限标记 unknown 保留 |
| `MAX_SCAN_HASH_BYTES_PER_ROUND` | `268435456`（256 MiB） | 正整数；后台扫描/复核到限即保留，不能用未完成 hash 授权 |
| `MAX_SCAN_DEPTH` | `64` | 1–256；超深未知保留，不跟随目录软链 |
| `MAX_ROUND_SECONDS` | `10` | 1–60；到期停止接新对象，未完成 trash 有记录地续作 |
| `STAGING_TTL_SECONDS` | `86400`（1 天） | 60–31536000，另需所有安全条件 |
| `OFFLINE_PROTECTION` | `true` | 源/重建事实不可确认则保留 |
| `OFFLINE_MODE` | `false` | 显式离线部署，启用保护时只接受 verified_offline 或系统内置材料确认 |
| `SHARED_CACHE_MODE` | `report_only` | 本轮只接受此值，显式显示不支持删除 |

有效配置通过同一个 cache factory 供五语言、统计和清理使用，不能继续在 prepare 中创建忽略配置的默认实例。删除预算不足的大项明确保留，管理员可调整预算后重试。

周期候选为超过 IDLE_TTL 的安全项。容量触发条件为高水位、磁盘余量或当前 reserve 不足；按 last-used 最旧优先，同时间大项优先，key 稳定排序。所有 GC 轮次取得 round 锁后，对 key 锁只能 try-lock，失败即跳过；执行器已持当前构建 key 时，对压力 round 锁也只能 try-lock，不等待，从而消除 round→key 与 key→round 倒置。每次 reserve 失败最多一个有界清理轮次后再 reserve 一次。无候选/不足分别暴露 `cache_no_safe_candidates`/`cache_capacity_insufficient`，保留原因计数，不循环、不强删。手动清理同样遵守 MIN_IDLE、pin、重建、Control/local 保护；不能借手动入口跳过安全条件。

### 7. Worker API and administrator entry

- 复用当前共享 Worker Token 认证，新增 capability `cache_governance_v1`，保持执行 protocol=3；旧 Worker 不领取治理命令。当前 Worker 处于同一可信管理域，不声称具备每 Worker 独立密码身份隔离；仍严格验证路由 worker ID、operation 归属、key 与 generation 一致性，普通账号/管理员会话不能替代 Worker Token。Control 不访问 Worker 文件系统，不接受客户端路径；只传非敏感 ID 和有界事实。
- Worker 定期发送最新缓存摘要与有界分页条目；Control 存最近快照、采样时间、完整性/游标、有效策略。离线/过期/部分快照明确标注，旧值不成为删除证据。
- 管理员读取 `GET /api/workers/{id}/cache`，创建 `POST /api/workers/{id}/cache/operations`（`preview|clean|protect|retry`），查询 `GET /api/workers/{id}/cache/operations/{operation_id}`。创建返回 202 与 operation ID；重复 idempotency key 返回同一操作，不叠加任务。`protect` 处理 pin/可重建确认，必须绑定最近所见 identity/digest，冲突返回 409。
- Worker 通过内部 `/api/workers/{id}/cache/commands/claim` 获取单个命令、`/cache/commands/{id}/result` 幂等回报，`/cache/snapshot` 上报只读信息；删除 guard 使用 `/cache/guards/acquire`、`/cache/guards/{operation_id}/check`、`/cache/guards/{operation_id}/result` 和 `GET /cache/guards` 有界枚举未完成项。具体 Pydantic DTO 名称可由实现确定，方法/路径与安全语义固定。
- 每 Worker 最多一个活动管理操作，其余返回 `409 cache_operation_in_progress`；周期、手动、压力共用 Worker 轮次锁。单操作最多 200 个显式 key，结果/原因字段有长度及条数限制；分页审计最多 100 条/页，终态操作默认保留 90 天且每 Worker 最多 1000 条，未完成操作/guard 永不按审计保留期删除。
- Worker 管理页的缓存区域展示采样时间、总量/版本/共享/staging/trash、预估可回收量、保留原因、实际策略；支持预览、清理、pin、明确可重建确认、查看与重试失败操作。预览提示“执行前会重新检查”，按钮只给管理员，后端使用现有管理员鉴权；普通账号不可越权，Worker 请求必须匹配操作归属。
- 审计记录管理员或 system、Worker、key、reason code、候选/已释放字节、结果、operation/generation 与时间。不会保存 Token、源凭据、用户代码、文件内容、宿主路径。静态 error code 用双语映射；UI build 不代替 Chrome 真实验收。

### 7.1 Explicit retry of failed Adapter cleanup

B 的 `retry` 接受与普通 operation 重试目标互斥的 `cleanup_id`，复用原请求；查询入口有界显示未完成 cleanup 的 ID、Adapter、status、attempts、稳定原因和 retry operation ID（最多 100 条/页及继续游标），即使尚无 guard 也可发现。管理员 protect 不自动重排失败 cleanup。

管理员事务按 `Worker → 管理 operation → WorkerCleanupRequest` 加锁，权威复核 failed、同 Worker/Adapter 且 Adapter 已删除；创建本组管理 operation，并将原 Request 置 pending、关联可空 `retry_operation_id`。attempts 不重置，旧失败原因进入审计。相同幂等 key/相同请求只返回原 operation，异参冲突；每 Worker 单活动管理操作仍生效。执行只走原 cleanups/claim/result，不再发布另一条 cache command。普通 GC 仍保护该未完成请求。

下一次 claim 单调加一（例如 failed/3 → pending/3 → running/4），回显 retry_operation_id。一次人工 retry 只获得一次额外逻辑尝试，真实保留/失败直接回 failed；预算续作和崩溃恢复保持同一 retry operation，不刷新失败额度。若旧删除 operation 停在失败 trash，先按同 cleanup_id 恢复原 operation/generation；管理员操作提供显式有限重试及审计，不解除任何保护。下一次真实失败后需新的管理员 retry。未知 pre-cache 仍不可强删。

关联任务 claim/result 先无锁读关联，再按 `Worker → 管理 operation → cleanup` 锁后复核领取和关联；成功/失败同时更新管理 operation 与 Request。重注册保持 Worker → cleanup；guard acquire 保持 guard → cleanup 且不回取 Worker/管理 operation 锁。聚合只无锁查询子 guard，避免反向锁环。未完成 Request 当前关联的 retry operation 不得被审计 retention 删除；换关联后的旧终态审计及已完成 Request 按常规保留策略处理。

### 8. Serial delivery and evidence

A 提交包含本地元数据/锁/删除、Control guard、新引用与恢复入口、旧路径收口、兼容和竞态测试，自动策略关闭。A 必须先过安全 Review/针对性测试，之后同组 B 提交策略/统计/协议管理入口/UI/分类验证，不需额外 PR 或用户重复确认。最后统一最终 head 静态检查、完整测试、独立 Review 与第三组真实 v3 运行证据。

旧 ready 升级必须先用基线真实准备并执行，保留原缓存，再用新代码运行同一版本，记录原 manifest/digest 不变、cache hit、无安装/下载、业务结果；不能用新安装成功替代。专用数据环境覆盖低容量旧版本释放后新版本成功，以及 queued/retry/recovery、竞争、离线、重启、未知 journal、共享边界与管理员浏览器交互。组内设计/Worker 子任务采用 LOCAL_FAST，不自行 push、建 PR 或部署；第三组整体 REMOTE_RELEASE，由集成负责人按已授权范围完成 PR/部署/合并。实际部署与最终用户验收未执行时如实保持未完成，不关闭 #139。

## Risks / Trade-offs

- 删除 guard 中断会暂缓该 Worker/version 的新引用 → 不自动过期；提供明确状态、有界恢复与同协议人工重试，优先保证已准备依赖不误删。
- 外部源普遍缺少可重复构建证明，可回收量可能低 → 默认 unknown、显式确认与 pin，并在界面解释原因，不把固定版本字符串当作证明。
- 大目录/大量小文件可能跨轮清理 → trash 继续计费、幂等记录、实际字节核对，预算内推进。
- 独占项锁延长到业务结束 → 当前同 Adapter 单 active 合同下无额外业务并发损失；不扩大到跨 Worker 共享 cache root。
- 新引用入口漏门禁会破坏协议 → A 独立审查必须枚举全部创建/Replay/Incident/恢复写路径与 SQL 并发测试，不能只测 API happy path。

## Migration Plan

1. 增量迁移新表与 capability，保留旧内容和卷；Control 先接受新协议，旧 Worker 不获新指令。
2. 同一 root 停止旧 Worker 并确认旧进程退出后升级，启动恢复先解释旧 journal，首次元数据初始化有界且不删除；A 安全门禁验证完成前自动开关保持 false。
3. 同组完成 B 并在专用数据环境显式调低阈值验证；按实际部署决定是否启用，用户环境默认仍为 false。
4. 回滚先关闭两个自动开关，停止新管理操作，排空/安全撤销现存 guard 与本地记录；只有不存在未完成 guard 时才能降级相关表。旧 manifest 保持可读，禁止清卷、伪造回执或强制解除 guard。
