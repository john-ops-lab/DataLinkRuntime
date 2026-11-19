# Worker Resource Reservations Specification

## Purpose

定义 Worker 实例容量、派发前预留和 Claim 的事务边界。Wave 2 第二批在隔离环境验证这些事务；生产以持久切换状态决定是否启用预留，停领、排空和容量对账后可显式启用。

## Requirements

### Requirement: 资源观测有身份和新鲜度
容量 SHALL 来自扣除Agent/系统余量后的真实宿主envelope，并带Worker实例、capacity_version和观测时间；默认15秒内的新鲜容量才可分配。实例资格 MUST 同时满足名义租约与已证明的保守前驱围栏，任何租约状态不明均拒绝新分配。未知、过期或未通过preflight MUST 拒绝新分配，不能把瞬时free或进程slots当成集群账本。

容量写入只接受可信验证器针对当前实例生成的宿主 envelope。宿主 CPU 向下、Agent/系统余量向上取整为毫核；按各维度扣减并以零为下限。普通注册、心跳及人工构造的测试样本均不能作为生产容量来源。启用前须接入真实宿主验证器并完成旧责任对账。Pool 选址与 Claim 按冻结输入核对材料：managed files 需要 `managed_files_v1`，内置 wheel 需要 `builtin_wheel_v1`；验证环境声明与管理员授予的成员材料范围均须具备相应能力。

#### Scenario: 第二次续租迟提交
- **WHEN** 已提交的续租候选尚在 nominal 和 guard 期限内，但 proof_before 已到，且没有独立持久的签发回执
- **THEN** 容量与 held Relay 仅接受未退役且数据库当前时间严格早于 min(nominal_until, guard_until, proof_before) 的候选；迟提交不能恢复资格

#### Scenario: 新实例沿用旧心跳容量
- **WHEN** 新instance尚未完成本地残留对账，却读到旧instance容量
- **THEN** 容量保持unknown/reconciling，不能接新Claim；不得直接将旧预留归零

#### Scenario: 容量下调
- **WHEN** 新有效envelope低于已占用量
- **THEN** 停止新分配并展示超额事实，不提前释放预留或终止合法在途业务

### Requirement: 首版按冻结 limit 保守预留
每任务 SHALL 以原standard资源快照预留CPU、memory、pids、tmp及一个execution slot，并验证nofile和隔离硬能力；同Worker的held、claimed和reclaiming合计 MUST 不超过allocatable后再接受新预留。Pool名和节点变更不得提高任务limit，不引入request/limit拆分。

CPU 预留 MUST 不低于 Worker 对该 limit 实际设置的 cgroup 配额，并向上取整至毫核；现有最低配额对应 10 毫核。不得通过低于最低配额的历史 limit 少记资源或改写历史快照。预留 SHALL 固定其 CPU 计费版本；活动预留版本不是当前版本或低于最低配额时，MUST 保留原记录并阻断该 Worker 的新预留，直至责任按对账合同收敛。版本字段不能替代旧写入者及数据库权限围栏。

#### Scenario: 并发争抢最后容量
- **WHEN** 两个Control为不同Adapter同时分配只够一个任务的Worker
- **THEN** 至多一份预留成功，另一任务持久等待，数据库账本与本地硬限制同时有效

### Requirement: 接受责任与等待派发保持原子性
接受Execution或事件T2 SHALL 同事务创建Admission和已计费的持久派发意图；未选择目标时不得发布无目标消息。等待意图和待发布消息 MUST 都占Outbox预算，选址/回收不能重复计费或先释放接入责任后仅存内存队列。

#### Scenario: 暂无兼容容量
- **WHEN** 已接受Execution尚无合法Worker
- **THEN** 保持queued及awaiting_target责任，attempt_count不变，可查询/取消/恢复；新接收仍受既有容量门禁

#### Scenario: T2 提交中断
- **WHEN** 事件关联、Execution或派发意图写入后事务失败
- **THEN** 全部回滚，原pending及容量保持，不存在无Outbox的半交接

### Requirement: 派发预留有界并在 Claim 原子确认
每Execution/generation SHALL 至多一份有效held预留，TTL为30秒且不能由Relay重试无限续期。Claim MUST 同事务复核路由、实例、维护状态、环境、Slot及容量，再将held转claimed、创建Attempt并增加计数。正常业务投递 SHALL 沿用v4的durable Claim、journal、Sandbox、结果确认、物理清理及责任结算后ACK顺序；重复/失效投递仍须由服务端权威确认才可ACK_NOOP。

held 绑定同一 Execution、派发 generation、Worker 身份、instance_id 与 instance_generation。Claim 后的占用及清理结算须进一步绑定同一 Attempt 及其 fencing_token；其他实例或 Attempt 的凭据不能结算这份预留。

#### Scenario: Confirm 不明后 held 过期
- **WHEN** 发布可能成功但确认丢失，held随后到期
- **THEN** 数据库先撤销旧预留/generation再重新选址，旧消息不能执行；迟到Confirm不能复活旧Outbox或抹掉责任

#### Scenario: Claim 响应丢失
- **WHEN** Claim已提交但Worker未收到响应，随后重投原消息
- **THEN** 不创建第二个有效Attempt、不重复扣资源；原Attempt按既有租约/journal合同恢复

### Requirement: 等待意图与发布结算

普通执行、单条 T2 和业务批次的接受事务 SHALL 为每个等待派发的 Execution 预付一个最大 dispatch envelope。选定目标时 MUST 在同一事务创建 held 并将原 Outbox 预付转为实际序列化字节。等待意图和待发布行都计入数量、字节与年龄门禁。

旧代消息进入 DLQ 时可持久接收 Incident。等待选址的责任不触发旧式缓存 reactivation；终止当前有预留的责任时必须锁定对应 Reservation 后核对。已 Claim 的物理清理责任继续按原合同结算。

#### Scenario: Relay 与重新选址交错
- **WHEN** 原代的发布结果未知且预留已失效，迟到的 Relay 收到 Confirm
- **THEN** 结算须核对 Outbox 租约、派发代和预留；旧行保持 superseded，新代的预留及待发布责任保持有效

#### Scenario: 生产切换前收到 Pool 请求
- **WHEN** 生产普通执行、T2、业务批次或补处理请求需要接受新的 Pool 责任
- **THEN** 服务端沿用原接收门禁，既有请求仍可按原权限和幂等规则读回，fixed 执行继续沿用既有派发与 Claim

### Requirement: 共享容量分配遵守既有锁序

分配事务 MUST 沿用 Adapter、订阅路径的 Subscription、Lease/Checkpoint、Receipt/Batch、订阅及全局 quota，再依次锁 AdapterAdmission、GlobalAdmission、Execution、Attempts、Adapter Slot 0、Pool、Worker/Capacity、Reservations、Incidents 和 Outbox。同类多行按稳定 ID 排序，Worker/Capacity 按 worker_id 排序。候选 MUST 在持容量锁时复核路由、管理员授权、实例资格、环境、硬能力及剩余资源。

5.2 的单次 Pool 成员选址按 Worker ID 升序检查最多 100 个已启用成员。每个等待的 Execution 持久记录扫描游标；本页未命中且还有后页时，下次从本页最后一个 ID 之后继续。扫描到末页后回绕，成功选址时重置游标。每页合格成员按最大维度占用率、最近一次分配时间、Worker ID 依次升序排名。跨 Adapter/Pool 的公平调度另按有界轮转合同执行。

#### Scenario: 维护与分配竞争
- **WHEN** 控制端选址或取消任务时，另一个事务更新维护状态或容量
- **THEN** 维护和容量更新只锁 Worker 后缀，扫描者另起事务依完整顺序重锁，Relay 在独立短事务取得 Outbox 租约并在提交后发布

### Requirement: 容量账本通过离线 schema 升级安装

从生产路由管理基线安装容量 schema 时 MUST 校验源发布、目标声明、连续历史意图和部署 owner 隔离状态。0091→0092 是离线 schema 升级。源必须是 `0091_issue129_pool_routing` 与 `f536419b59282891cf537e2d8fef65b7e8b92d1c`，目标为 `0092_issue129_worker_capacity`。owner 执行 check，确认 runtime 角色已禁用登录、其他写入者及活动责任均已排空，再由 owner 执行 apply；迁移、authority 更新与回执在同一事务提交，另开连接 observe 读到 `committed_current` 后才确认当前升级成功。完整历史链在已有各层之后追加最早一对 `--first-source` 和 `--first-intent`，按连续前缀提供。此次升级仅安装账本与事务能力，生产切换仍按 5.5 及第 6 组执行。

#### Scenario: 从最长历史链升级
- **WHEN** 部署已保有七个连续的前驱回执，owner 准备安装容量账本
- **THEN** CLI 接受完整七对历史参数，并逐跳核对声明、意图与回执；缺少最早一对、跳级或源 SHA 错误时拒绝升级

### Requirement: Claim 前等待不消耗执行重试
新v4责任 SHALL 在每个领取周期冻结等待期限，默认900秒、允许30–86400秒；容量扫描不创建Attempt。周期最多10次实际held选址，达到后暂停自动派发并显示placement_limit_reached；到期仅在无活动Attempt时expired。reroute不得重置期限；实际失败后的合法Retry才开始新周期。

#### Scenario: 满载后增加兼容 Worker
- **WHEN** Pool满载期间原任务持续等待，随后在期限内扩容
- **THEN** 原attempt_count仍为0，ready后按验收负载10秒内开始Claim；执行后仅有真实Attempt记录

#### Scenario: Claim 接收预算不足
- **WHEN** v4 Worker的本地接收预算不足，或Control确定实际Claim响应不能在协商预算内处理
- **THEN** 不提交Attempt或消费执行重试；已held责任按原generation、TTL、placement次数和领取期限收敛，旧v3责任不追溯自动截止

#### Scenario: 等待到期或人工取消
- **WHEN** 无活动Attempt的任务到达冻结截止或被合法取消
- **THEN** 原子进入expired/cancelled并幂等释放未领取预留和Admission；事件失败屏障及恢复数据按原规则保留，不伪造业务成功

#### Scenario: 旧责任与未派发事件
- **WHEN** 迁移旧v3排队Execution，或Receipt尚未形成Execution
- **THEN** 不追溯增加自动过期；旧任务保留legacy无新deadline标记，事件沿原pending合同；仍限制每次held期限及自动选址次数

### Requirement: Claim 后准备失败保留真实尝试和有限预算
本地资源检查失败 SHALL 保存failed Attempt及resource_capacity_unavailable，按原冻结Retry Policy计入max_attempts；不得删除Attempt或回退attempt_count。节点 MUST quarantine至少30秒并完成新鲜容量对账才可重选；退避和最大次数保持原策略，不引入无限准备重试。

#### Scenario: 心跳后本机容量漂移
- **WHEN** DB已准许Claim，但实际本地资源无法预留
- **THEN** 记录一次真实准备失败、正常退避和节点回避，清理/资源账可对账，业务代码不启动

#### Scenario: 准备失败耗尽
- **WHEN** 多次真实准备失败耗尽原max_attempts
- **THEN** 按原失败/死信及文件Hold合同终结，原失败记录仍可见，不因“不算业务执行”继续无限尝试

### Requirement: 故障转移遵守所有权和恢复宽限
已Claim的Pool任务 SHALL 仅在旧Attempt失效并被fencing后按Retry Policy恢复；新v4执行不得早于旧lease到期加冻结recovery grace。旧实例不得在宽限中恢复业务权限；fixed不得隐式转移。相同Execution MUST 始终至多一个有效活动Attempt。

#### Scenario: Worker 网络隔离后恢复
- **WHEN** 新节点已合法领取后旧节点恢复上报
- **THEN** 拒绝旧result/progress及资源修改，保留新Attempt和旧worker_lost事实；目标端副作用按稳定业务键对账

### Requirement: 逻辑终态不等于物理容量已释放
资源释放 MUST 幂等且绑定自己的reservation/Attempt。清理或进程状态不明时 SHALL 保持reclaiming并禁止该节点新分配；只有实际停止/清理证明及容量对账，或有审计的节点隔离处置，才可归还容量。租约失效或Control重启不能单独证明物理回收。

#### Scenario: 业务成功但清理挂起
- **WHEN** 业务终态已持久化而本机资源仍被清理工作占用
- **THEN** 业务成功保持，资源继续计入reclaiming并显示阻塞；不能用重复结果提前释放

#### Scenario: 取消和 Reconciler 并发
- **WHEN** cancel、result及资源扫描同时收敛同一Attempt
- **THEN** 只结账一次，不能释放后来generation/Attempt的容量或修改已确定业务结果

### Requirement: 调度公平且不会无限热重投
调度 SHALL 在可运行Adapter/Pool间有界轮转，处理最多100候选/50毫秒且数据库等待另有限时；无容量项采用1至30秒退避并可被容量恢复唤醒。通知丢失 MUST 可由持久状态恢复；blocked头部不得永久占满其他可运行任务的机会。

#### Scenario: 受限 Pool 堵塞但其他 Pool 可运行
- **WHEN** 某Pool没有兼容节点，其他Pool有容量且≤10个可运行Adapter
- **THEN** 后者在冻结公平验收条件内分别获得Claim机会，前者保留明确原因和期限，不靠MQ热重投消耗所有资源

### Requirement: 预留模式切换保持原有责任可恢复
Control MUST 在默认关闭的预留模式与启用模式之间持久记录切换进度。切换须停止新 Claim、排空已领取责任、核对物理容量及旧消息，再统一启用各入口的预留要求；失败后保持可查询且可继续的切换状态。

#### Scenario: 升级时旧消息已发布尚未领取
- **WHEN** 对账发现原派发代仍可能投递
- **THEN** 原目标取得同代合法预留，或在重派前先失效旧代并保留已计费的等待意图；迟到消息不能产生第二份有效 Attempt
