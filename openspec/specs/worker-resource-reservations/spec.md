# Worker Resource Reservations Specification

## Purpose

定义 Worker 实例容量、派发前预留和 Claim 的事务边界。Wave 2 第二批在隔离环境验证这些事务；生产 Pool 接收及既有 fixed Claim 的切换须待停领、排空和容量对账完成。

## Requirements

### Requirement: 资源观测有身份和新鲜度
容量 SHALL 来自扣除Agent/系统余量后的真实宿主envelope，并带Worker实例、capacity_version和观测时间；默认15秒内的新鲜容量才可分配。实例资格 MUST 同时满足名义租约与已证明的保守前驱围栏，任何租约状态不明均拒绝新分配。未知、过期或未通过preflight MUST 拒绝新分配，不能把瞬时free或进程slots当成集群账本。

容量写入只接受可信验证器针对当前实例生成的宿主 envelope。宿主 CPU 向下、Agent/系统余量向上取整为毫核；按各维度扣减并以零为下限。普通注册、心跳及人工构造的测试样本均不能作为生产容量来源。启用前须接入真实宿主验证器并完成旧责任对账。Pool 选址与 Claim 按冻结输入核对材料：managed files 需要 `managed_files_v1`，内置 wheel 需要 `builtin_wheel_v1`；验证环境声明与管理员授予的成员材料范围均须具备相应能力。

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

#### Scenario: Confirm 不明后 held 过期
- **WHEN** 发布可能成功但确认丢失，held随后到期
- **THEN** 数据库先撤销旧预留/generation再重新选址，旧消息不能执行；迟到Confirm不能复活旧Outbox或抹掉责任

#### Scenario: Claim 响应丢失
- **WHEN** Claim已提交但Worker未收到响应，随后重投原消息
- **THEN** 不创建第二个有效Attempt、不重复扣资源；原Attempt按既有租约/journal合同恢复

### Requirement: 等待意图与发布结算

普通执行、单条 T2 和业务批次的接受事务 SHALL 为每个等待派发的 Execution 预付一个最大 dispatch envelope。选定目标时 MUST 在同一事务创建 held 并将原 Outbox 预付转为实际序列化字节。等待意图和待发布行都计入数量、字节与年龄门禁。

#### Scenario: Relay 与重新选址交错
- **WHEN** 原代的发布结果未知且预留已失效，迟到的 Relay 收到 Confirm
- **THEN** 结算须核对 Outbox 租约、派发代和预留；旧行保持 superseded，新代的预留及待发布责任保持有效

#### Scenario: 生产切换前收到 Pool 请求
- **WHEN** 生产普通执行、T2、业务批次或补处理请求需要接受新的 Pool 责任
- **THEN** 服务端沿用原接收门禁，既有请求仍可按原权限和幂等规则读回，fixed 执行继续沿用既有派发与 Claim

### Requirement: 共享容量分配遵守既有锁序

分配事务 MUST 沿用 Adapter、订阅路径的 Subscription、Lease/Checkpoint、Receipt/Batch、订阅及全局 quota，再依次锁 AdapterAdmission、GlobalAdmission、Execution、Attempts、Adapter Slot 0、Pool、Worker/Capacity、Reservations、Incidents 和 Outbox。同类多行按稳定 ID 排序，Worker/Capacity 按 worker_id 排序。候选 MUST 在持容量锁时复核路由、管理员授权、实例资格、环境、硬能力及剩余资源。

#### Scenario: 维护与分配竞争
- **WHEN** 控制端选址或取消任务时，另一个事务更新维护状态或容量
- **THEN** 维护和容量更新只锁 Worker 后缀，扫描者另起事务依完整顺序重锁，Relay 在独立短事务取得 Outbox 租约并在提交后发布

### Requirement: 容量账本通过离线 schema 升级安装

从生产路由管理基线安装容量 schema 时 MUST 校验源发布、目标声明、连续历史意图和部署 owner 隔离状态。0091→0092 是离线 schema 升级。源必须是 `0091_issue129_pool_routing` 与 `f536419b59282891cf537e2d8fef65b7e8b92d1c`，目标为 `0092_issue129_worker_capacity`。owner 执行 check，确认 runtime 角色已禁用登录、其他写入者及活动责任均已排空，再由 owner 执行 apply；迁移、authority 更新与回执在同一事务提交，另开连接 observe 读到 `committed_current` 后才确认当前升级成功。完整历史链在已有各层之后追加最早一对 `--first-source` 和 `--first-intent`，按连续前缀提供。此次升级仅安装账本与事务能力，生产切换仍按 5.5 及第 6 组执行。

#### Scenario: 从最长历史链升级
- **WHEN** 部署已保有七个连续的前驱回执，owner 准备安装容量账本
- **THEN** CLI 接受完整七对历史参数，并逐跳核对声明、意图与回执；缺少最早一对、跳级或源 SHA 错误时拒绝升级
