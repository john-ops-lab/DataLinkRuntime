# Spec Delta

## Purpose

定义通用事件订阅的绑定、启停、配置、所有权和管理呈现，使不同来源共享可解释的生命周期，同时保护普通 Adapter 的运行方式、权限和已经接管的处理责任。

## ADDED Requirements

### Requirement: 订阅独立绑定普通 Adapter
系统 SHALL 让每条订阅绑定一个 Task/manual Adapter，并限制每个 Adapter 至多一条未删除订阅。新订阅 MUST 默认为停用；未实现或未通过交付验收的来源 MUST 不可启用。

#### Scenario: 重复绑定
- **WHEN** 两个请求同时为同一 Adapter 创建订阅
- **THEN** 至多一个成功，另一请求返回稳定绑定冲突，且不会启动第二个监听器

#### Scenario: 尝试启用未来来源
- **WHEN** 用户提交尚未交付的 MQTT 来源
- **THEN** 系统返回 `source_not_available`，不建立连接或创建执行

### Requirement: 停用保留已接管责任
停用订阅 SHALL 停止新接收、撤销旧监听所有权并回收连接；已提交的待处理责任 SHALL 按来源合同继续调度执行或由独立授权动作显式结清；停用本身不构成处置。仍有待处理、非终态关联执行、未闭合Batch、待控制意图或未结预留时 MUST 显示排空状态，包括尚无Receipt的空批次；不能把停用或state=failed解释为责任已闭合。功能关闭后既有控制责任SHALL继续由有界维护恢复，不依赖失效owner写入。

#### Scenario: 接收事务与停用竞争
- **WHEN** 停用与一条接收事务并发
- **THEN** 该事务要么在停用生效前完成并保留责任，要么因旧所有权拒绝且不确认来源，不能出现已确认但无责任的数据

#### Scenario: 排空中的配置更新
- **WHEN** 监听已关闭但尚有已接管任务时用户修改范围或删除订阅
- **THEN** 操作返回 `subscription_draining`，旧任务继续使用原快照

### Requirement: 配置和激活版本明确
影响运行的配置写入 SHALL 使用 expected config version；启用时 SHALL 固定当时的 Adapter Revision、Worker 与凭据绑定引用。启用或排空期间 MUST 禁止影响运行的普通保存，名称、描述和本规格定义的受限代码修复除外；凭据真值不属于版本快照。

#### Scenario: 旧页面提交
- **WHEN** 用户以过期版本修改订阅
- **THEN** 返回 `subscription_config_conflict`，当前配置、游标和任务不变

#### Scenario: 重新启用使用新代码
- **WHEN** 订阅停用排空后保存新代码并重新启用
- **THEN** 新接收使用新激活版本，已有历史事件及其默认补处理保留原输入和版本

#### Scenario: 已成功闭合完成链的管理入口
- **WHEN** 对成功确认且已闭合的 K8s completed 提交新的原/repair/current-config replay
- **THEN** 拒绝并引导显式新完整批次，不重开旧完成责任；同一原请求仍按当前 ACL 和完整原参数先只读回原 Delivery，再对新请求校验当前版本/资格，合法配置升版或 featureoff 不阻断旧请求读回；普通对象/WATCH 历史补处理规则不变

### Requirement: 监听接管具有唯一有效所有权
系统 SHALL 为监听接管分配可失效的所有权和单调 generation；只有当前有效 owner 能提交接收责任、合并待处理和推进来源位置。恢复过程 MUST 不长期占用普通 Execution/Attempt 槽位。

#### Scenario: 旧进程恢复运行
- **WHEN** 租约过期后新进程已接管，而旧进程继续提交
- **THEN** 旧提交被拒绝并关闭旧连接；来源确认仅能重复已持久化安全位置，不能越过尚未接管的数据

### Requirement: 带积压的失败事件可受限修复
订阅停用且进入显式 repair 状态后，系统 SHALL 暂停新派发并等待已有非终态执行收敛，再允许保存仅改变代码/依赖的不可变修复 Revision。该版本只可被明确选择用于阻塞事件的新补处理，MUST 不改原激活、原执行或后续 pending 引用，也不得改变 Worker、输入、凭据和来源配置。

#### Scenario: 代码缺陷阻塞大量后续事件
- **WHEN** 前项因代码错误失败且后续 pending 尚未排空
- **THEN** 操作员可停用进入 repair、保存并选择修复 Revision 补处理前项；成功后恢复排空，无需先跳过原事件

### Requirement: 配置切换和复制不隐式重置进度
系统 SHALL 将来源身份和 epoch 与一般配置版本区分。范围、来源或起点变更 MUST 在停用排空后明确创建新 epoch 并展示重读/缺口影响；K8s server或有效CA信任范围变化同样MUST显式新epoch，不能只换scope指纹并复用旧cursor；克隆 MUST 默认停用且不复制进度、责任、批次或消费组占用。

#### Scenario: 凭据轮换
- **WHEN** 来源凭据轮换后重连且源身份没有变化
- **THEN** 使用同一 epoch 和持久进度，不把重复消息视为新源事件

#### Scenario: Kubernetes 信任范围变更
- **WHEN** K8s server或有效CA信任范围改变，而没有显式new_epoch
- **THEN** 拒绝更新；只有停用、原生关闭、旧责任结清及显式新epoch后才可切换，旧历史保留，同引用token值轮换仍沿原epoch/RV

#### Scenario: 克隆 Kafka 订阅
- **WHEN** 用户复制一条已启用订阅
- **THEN** 副本停用、消费组独立且无 cursor，首次启用必须选择起点

### Requirement: 放弃完成确认有独立权限和期望责任校验
专用完成放弃 SHALL 仅允许有edit权限的认证主体在desired disabled、原生监听确认关闭且该订阅关联Execution全终态时发起；queued/running/retry_wait任一存在MUST拒绝。请求MUST绑定expected_config_version、Batch/epoch、completed Receipt、期望最新Delivery ID/责任序号及Execution关联快照（可为null）、actor、非空reason、request_id和明确影响确认。未冻结、已成功、已被其他处置终结、跨批/epoch或最新责任/执行关联变化MUST冲突且零副作用；仅原请求按同参数和当前权限幂等读回。UI SHALL 明示业务结果未确认、目标可能部分执行且不可回滚、将取消尚未派发的本次补处理和后续新批核对要求。

#### Scenario: 旧确认框提交或越权放弃
- **WHEN** 只读用户发起专用放弃，或确认后config/batch/receipt/latestDelivery及执行关联发生变化
- **THEN** 返回权限/稳定冲突错误，不结账、不推进屏障、不创建或终止任何责任；latest Execution已succeeded但Receipt/Delivery投影仍processing时也据真实执行结果拒绝

#### Scenario: 同请求重复提交
- **WHEN** 已成功放弃后以相同actor、对象、原因及期望责任重试同request_id
- **THEN** 返回同一次结账结果，不重复释放pending或reserve、追加审计；改变任一语义字段复用该request_id则冲突

### Requirement: 人工完成结账不隐式修改来源或其他责任
专用放弃 SHALL 仅解除completion屏障，重算并保留其他普通失败/active屏障及原repair入口，让旧WATCH按持久原序排空；MUST不自动enable或新LIST，不变更旧activation、配置、epoch、Worker、凭据或Saved Input。旧责任全结清后才可沿已有配置路径调整收紧限额并显式启用新批；不得新增带积压限额API或恢复信用替代本处置。

#### Scenario: 完成放弃后仍有普通失败
- **WHEN** 完成责任已放弃而旧成员/WATCH仍有失败或pending
- **THEN** 运行锁与普通失败repair入口保留，来源保持停用；逐项结清后才解除运行锁并允许用户显式启用新批

### Requirement: 状态区分接收和处理
系统 SHALL 独立显示启停意图、连接状态、处理状态、数量/字节积压、最老待处理时间和最近错误，并关联来源事件、Execution 与补处理链。连接成功、已经接管或专用人工放弃 MUST 不显示为业务处理成功；完成冻结、完成成功、批次失效和业务结果未确认的人工结账 SHALL 可区分。

#### Scenario: 来源正常而目标失败
- **WHEN** 监听正常但 Adapter 已 dead_letter
- **THEN** 页面显示连接正常及处理失败/顺序阻塞，并能定位具体事件和执行

### Requirement: 权限与脱敏沿用 Adapter 边界
订阅读写 SHALL 分别服从绑定 Adapter 的 view/edit 权限及凭据引用权限；全局查询 MUST 在分页与统计前按权限过滤。Secret 真值 MUST 不进入 Envelope、浏览器响应、普通日志或 AI 自动上下文；用户文案 SHALL 支持中英文而机器错误码保持稳定。

#### Scenario: 无权限用户猜测事件 ID
- **WHEN** 无绑定 Adapter 查看权限的用户请求订阅、事件或重放详情
- **THEN** 系统拒绝且不暴露内容、源地址或数量信息

#### Scenario: 只读用户补处理
- **WHEN** 只有 view 权限的用户调用 replay、skip、reset或专用完成放弃
- **THEN** 系统拒绝，不创建执行也不推进屏障
