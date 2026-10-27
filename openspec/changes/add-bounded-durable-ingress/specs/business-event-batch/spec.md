# Spec Delta

## Purpose

定义显式选择的有界业务批次，使多个已接管事件可以共享一次执行，同时逐条保留稳定身份、首次输入、不可变处理绑定和业务结果；明确成员处置、共享取消、责任交接、容量计费及保留清理边界，保护默认单条模式的既有语义。

## ADDED Requirements

### Requirement: 业务批次显式选择并固定输入绑定
系统 SHALL 仅允许 HTTP/RabbitMQ 显式选择 business_batch；旧 per_event 默认、身份和输入形状 MUST 保持。批次 SHALL 只聚合同 Adapter、订阅、epoch、shard、有效 Activation 和 Revision override 的可运行前缀，最多 100 成员、完整输入 256 KiB、组装等待不超过 50 ms。代码版本 MUST 显式声明支持 dlr.event.batch.v1。

#### Scenario: 改为多条处理
- **WHEN** 用户修改 mode 或配置新批次代码
- **THEN** 必须停止、确认关闭并排空，保存新配置/激活；旧责任不被改成新输入

### Requirement: 批次保留每条责任并原子交接
T2 MUST 在同一事务创建一份 Execution/Admission/Outbox、不可变 manifest 和全部成员关联，转 processing 并扣减 N 条 pending；失败全部回滚。数据库 SHALL 保留旧单条唯一约束，禁止同 Execution 混用单条/批次，约束成员身份、连续索引、精确 count/manifest 和关联不可改写。同 Adapter MUST 仍至多一个 active Attempt。

#### Scenario: 部分关联或并发登记
- **WHEN** 事务遗漏成员、写错 Receipt、同时登记普通关联或提交前退出
- **THEN** 数据库拒绝不完整提交，原责任、绑定和预算保留，不留下部分批次

### Requirement: 严格验证结果后才提交共享业务终态
结果 MUST 精确覆盖原 manifest，整体不超过 min(64 KiB,512+N×512 bytes)，仅允许全部成功或成功前缀/首个失败或未知/后继未执行。系统 MUST 在原权威 Attempt 终态事务内检查身份、结构、索引及 Worker/Control 截断；任何不可信结果不得保留其成功前缀。全部成员成功才允许 Execution succeeded；非成功为明确业务失败，安全成员事实同事务持久化。

#### Scenario: 缺失或损坏结果
- **WHEN** 成功回报缺成员、错 hash、重复索引、超限或经脱敏/截断破坏
- **THEN** Control 将全体标为未知并提交业务失败，不先成功后翻案

#### Scenario: 基础设施故障仍可重试
- **WHEN** Attempt 故障按原策略应进入 retry_wait
- **THEN** 不封存最终成员结果、不释放 Admission，沿原 Execution/输入恢复；不能把临时故障当最终未知而吞掉恢复

### Requirement: 成员处置与共享取消可解释
每条 Receipt/Delivery SHALL 保留独立成功、失败、未知或未执行投影，blocker 为最早未结清责任。重放/修复/跳过 MUST 保留旧事实并新增 ordinal 和预算；单成员重放 MUST 创建只含目标成员的新 batch 输入和 manifest，成员索引从 0 开始，结果只覆盖该新 manifest，不得夹带其他待处理事件。跨 mode 的当前配置重放拒绝。共享取消 SHALL 说明影响范围，只保留 Control 已验证持久事实；无可信成员结果时，可能已启动的成员为 unknown，仅可证明从未启动的成员可为 not_run，不得依据目标写入或日志推断成功。取消请求本身不得提前释放尚未终态的共享 Execution 所占 Admission；Execution 终态后按原规则幂等释放，非成功成员责任、blocker 和恢复保留预算不因此结清。

#### Scenario: 部分成功后恢复
- **WHEN** 首失败成员单独重放成功但后继仍未执行
- **THEN** 原成功/失败事实不覆盖，后继责任仍未结清，不能显示整批全部完成

#### Scenario: 单成员重放与新事件排队
- **WHEN** 一个批次成员单独重放，且同一来源还有其他待处理事件
- **THEN** 重放输入和 manifest 仅含该目标成员，结果按这一成员验证；其他事件保留原有顺序和独立责任

#### Scenario: 未启动的批次取消
- **WHEN** Control 确认共享 Execution 从未启动并完成取消
- **THEN** 成员记为 not_run，原 Admission 按终态规则释放，未结清责任和恢复所需预算保留

#### Scenario: 运行中的批次取消且无可信成员结果
- **WHEN** 用户请求取消已启动或可能已启动的批次，Control 未确认成员成功
- **THEN** 请求本身不提前释放 Admission；权威终态后成员记为 unknown，Admission 幂等释放，未结清责任、blocker 和恢复所需预算保留

#### Scenario: 取消与合法全成功回报同时到达
- **WHEN** 取消请求按原锁序优先，但 Control 同事务严格验证全部成员成功
- **THEN** Execution 可保持 cancelled；已确认的成员成功事实保持，不能因逻辑取消改为未知或未执行

### Requirement: 共享副本与预算整体保留和清理
T1 MUST 预付单成员完整未来输入及有界关联/结果预算，T2 的成员份额合计覆盖真实共享数据；不得重复免账。成功 Receipt 原文可按 7 天清理，任一原成员未解决、重放未完或仍在保护期时，共享输入和固定关联 SHALL 受保护。普通历史清理 MUST 要求共享 Execution 已为终态并已固定成员结果，所有相关 Receipt 均为 succeeded、skipped、superseded 或 abandoned 且各自终态时间已满 30 天；因此从最后一个成员达到终态起满 30 天才能整体清理共享数据并幂等释放。受控永久删除适配器时，订阅必须已删除、停用、关闭且全部责任排空，才可在删除事务中跳过 30 天等待并整体清理已结清批次；不得删除未完成责任。旧单条清理 MUST 不逐成员破坏共享输入。

#### Scenario: 成员先后达到终态
- **WHEN** 同批成员均已结清，但最后一个成员达到终态尚未满 30 天
- **THEN** 普通清理保留整个共享批次；最后一名成员的保护期届满并满足其他条件后才能整体清理

#### Scenario: 永久删除已排空的适配器
- **WHEN** 用户永久删除适配器，相关订阅已删除、停用、关闭且责任全部排空
- **THEN** 同一删除事务可清理已结清批次而不等待 30 天；任一未完成责任仍须阻止删除

#### Scenario: 一个成员到期而另一个失败
- **WHEN** 成功成员原文或原 30 天窗口到期，但另一成员仍失败或重放中
- **THEN** 仅释放真正删除的独立原文，共享输入/manifest/关联和其份额保持，旧版本不能误作单条结算或删除
