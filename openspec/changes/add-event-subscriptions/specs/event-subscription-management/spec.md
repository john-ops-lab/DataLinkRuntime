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
停用订阅 SHALL 停止新接收、撤销旧监听所有权并回收连接；已提交的待处理责任 SHALL 继续调度和执行。仍有待处理或非终态关联执行时 MUST 显示排空状态，不能把停用解释为取消执行。

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
系统 SHALL 将来源身份和 epoch 与一般配置版本区分。范围、来源或起点变更 MUST 在停用排空后明确创建新 epoch 并展示重读/缺口影响；克隆 MUST 默认停用且不复制进度、责任、批次或消费组占用。

#### Scenario: 凭据轮换
- **WHEN** 来源凭据轮换后重连且源身份没有变化
- **THEN** 使用同一 epoch 和持久进度，不把重复消息视为新源事件

#### Scenario: 克隆 Kafka 订阅
- **WHEN** 用户复制一条已启用订阅
- **THEN** 副本停用、消费组独立且无 cursor，首次启用必须选择起点

### Requirement: 状态区分接收和处理
系统 SHALL 独立显示启停意图、连接状态、处理状态、数量/字节积压、最老待处理时间和最近错误，并关联来源事件、Execution 与补处理链。连接成功或已经接管 MUST 不显示为业务处理成功。

#### Scenario: 来源正常而目标失败
- **WHEN** 监听正常但 Adapter 已 dead_letter
- **THEN** 页面显示连接正常及处理失败/顺序阻塞，并能定位具体事件和执行

### Requirement: 权限与脱敏沿用 Adapter 边界
订阅读写 SHALL 分别服从绑定 Adapter 的 view/edit 权限及凭据引用权限；全局查询 MUST 在分页与统计前按权限过滤。Secret 真值 MUST 不进入 Envelope、浏览器响应、普通日志或 AI 自动上下文；用户文案 SHALL 支持中英文而机器错误码保持稳定。

#### Scenario: 无权限用户猜测事件 ID
- **WHEN** 无绑定 Adapter 查看权限的用户请求订阅、事件或重放详情
- **THEN** 系统拒绝且不暴露内容、源地址或数量信息

#### Scenario: 只读用户补处理
- **WHEN** 只有 view 权限的用户调用 replay、skip 或 reset
- **THEN** 系统拒绝，不创建执行也不推进屏障
