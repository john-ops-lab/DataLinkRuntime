# control-ha Specification

## Purpose

定义双 Control 的注册、请求权限、健康探测、后台责任及来源租约接管。三类文件的跨节点共享和后端接管由各自规格另行定义。

## Requirements

### Requirement: 同镜像 Control 分别注册并受部署权威约束
显式双节点部署 SHALL 使用相同发布镜像、schema、部署权威及已验证认证材料。两个固定位置各自至多登记一个有效进程，注册竞争须由数据库串行裁决。业务事务 MUST 验证本次进程的身份、令牌、有效租约及部署 epoch。运行角色、Worker 凭据隔离和原独占升级权限继续有效。

#### Scenario: 并发登记或重启
- **WHEN** 两个匹配实例登记不同位置、另一个进程争用已占位置，或过期进程在位置被重新登记后尝试写入
- **THEN** 合法实例分别持有独立身份，争用者不能占用有效位置或启动 intake，过期身份不能续行原操作。

#### Scenario: 离线升级 0093 到 0094
- **WHEN** 管理员从规定的 W2 发布来源停止所有写入者并执行升级
- **THEN** 使用独占 owner 和原运行角色隔离条件，在原数据库保留集群身份、Worker 责任及原权限，以新发布身份登记；旧实例不能用先前授权恢复写入。

### Requirement: Worker 可通过统一入口跨节点办理请求
Worker SHALL 沿用原协议和认证，通过同一入口办理注册、心跳、Claim、续租、纯 JSON 结果及清理。接收 Control MUST 对 Worker、实例、generation、Attempt 及证明执行原有校验，业务请求无需客户端选择 Control。

#### Scenario: Claim prepare 与 finalize 落在不同实例
- **WHEN** A 生成带静态位置标志的 43 字符 opaque proposal，B 收到 Worker 原样回传的 finalize
- **THEN** B 仅向预配置且通过 TLS 验证的同部署 HTTPS 对端转交一次；A 校验完整随机 proposal、身份及原截止时间。路由标志不授权，转交不接受客户端目的地、重定向或非幂等自动重发。

#### Scenario: Claim A、结果 stage B 与 finalize A 分阶段回收
- **WHEN** 双节点纯 JSON 请求依次在 A 完成 Claim、在 B 持久暂存结果，再由 A 接收 finalize
- **THEN** 双节点纯 JSON 模式中，Claim 已知持久提交后，原节点须在本阶段原有本地截止前确认自己的 child 实际退出并回收，再返回 Attempt proof。结果 stage 已持久化后也须在原截止内回收该阶段 child，再返回已签名 intent proof。任一节点在空闲且有额度的槽中为 finalize 启动新的有界 child，核对原 nonce、签名、正文 MAC、Worker/实例/generation、Attempt、持久 intent 和原证明有效期；新 child 不延长证明有效期。无效或迟到的 finalize 不关闭其他进程；回收未知保留占用与持久责任，不返回声称本阶段已完成的证明。默认单 Control 保留原通道及文件支持。

### Requirement: LB、管理入口和能力健康分别定界
LB SHALL 使用 `/api/health/ready` 判定节点能否承担数据库及身份相关请求，`/api/health/live` 单独报告进程活性。`/api/health` 保留聚合诊断并分别显示本节点、集群登记及功能状态。账户、管理 token、CSRF、来源与 Worker 的入口授权 MUST 沿用原规则；外部传入的私有路由或身份字段不得被当成可信上游。

#### Scenario: 同名本地卷与共享挂载替换
- **WHEN** 两节点配置相同路径甚至复制了相同 marker，或某节点的已验挂载/本次实例身份发生替换
- **THEN** 每类须分别验证部署、backend kind、namespace 及两端当前身份，使用本轮跨节点挑战/应答验证实际可见性、原子发布、fsync 和互斥后才采用。应用不得在空挂载自动生成可用证明；失败时拒绝本类共享操作并保留原占用，正常的其他能力仍按各自条件服务。

#### Scenario: Broker 或单项后端失效
- **WHEN** Broker 不可用，但数据库可提交且身份有效
- **THEN** 经真实 LB 的合法续租、纯 JSON 结果和 Receipt 读回可继续；新接收和 MQ 发布按各自条件拒绝。双节点分别判定 Managed Files、内置材料与 AI 临时附件；只有本类共享后端的部署身份、有效绑定和实际共享预检均通过时才开放其路径。 HA 新接收与发布使用最近成功的 Broker 活性观测，约每 5 秒探测，观测超过 10 秒视为不可用，已知故障立即关门；恢复须有新的成功观测和原能力条件。

#### Scenario: Worker 验证 Claim 证明后在 staging、构建或执行期间失去 AMQP 连接
- **WHEN** Worker 已收到 delivery 并验证原 Claim 证明，此时在已有 staging、受监督构建（包括 Go）或业务执行中发生 AMQP 连接中断，而数据库、HTTP 管理入口、身份与租约仍有效
- **THEN** Worker MUST 停止新领取，保留同一受监管 Attempt，分别遵守原准备与执行截止；无须等待 start_execution 返回，并允许合法 HTTP 续租、纯 JSON 结果和清理。连接丢失本身不取消业务；尚未验证 Claim 证明、真正停机、取消、失权及任何未知责任仍按原合同停止或对账。原 channel 的 delivery tag 不得在新 channel 确认。正常重连后每次重投须以当前投递及权威状态判定 ACK_NOOP，不得重复执行业务。

#### Scenario: 诊断与接管共用节点预算
- **WHEN** 两个位置分担启用订阅，节点收到新的连接诊断请求
- **THEN** 启用来源接收的双 Control 每节点固定为 owner 保留全部 16 个共享原生名额，当前启用数不足 16 时也不出借。在线原生诊断直接返回容量繁忙，不为名额裁决查询启用数或启动 SDK；既有工作实际结束后才释放占用。来源接管时限按对应协议另验。

#### Scenario: 数据库不可提交或长连接中断
- **WHEN** 数据库只读或不可用，或者客户端正在使用的 SSE/Assist 连接中断
- **THEN** 数据库不可提交的节点 ready 返回 503，新责任不能被报告为已接受。长连接中断对用户可见；查询或经用户发起的重试沿原权限进行，断连不自动重发 Provider 请求。

### Requirement: 后台职责有持久并发与接管合同
Scheduler、Outbox、Retry、Attempt/资源对账、配额audit、DLQ、T2/批次结算、retention、Worker 缓存/Adapter 清理和拓扑检查 SHALL 按职责使用可重领责任、唯一约束或带generation的单owner。MUST 不通过“部署只启动一份”保证HA；内存通知/游标丢失必须有有界持久扫描恢复。

#### Scenario: 扫描后进程退出或候选前缀长期受阻
- **WHEN** Scheduler、fixed/reserved Retry、Attempt 或 Reservation 扫描选择候选，进程在处理前退出，或部分候选暂时无法取得业务锁
- **THEN** 有界扫描继续访问后页并可绕回重查，领取方在原业务锁序内重检条件和 generation；计划点、有效派发、Attempt 及预留各自遵循原有唯一约束。发布租约和迟到 Confirm 不得使已被替代的责任复活。

#### Scenario: Worker 清理命令或对账在节点切换时仍未完成
- **WHEN** Control 在请求提交后退出，另一节点接办，随后旧 Worker 回执到达
- **THEN** 缓存与 Adapter 清理按原请求标识、实例和操作代次核对，未证实物理完成的责任保留，旧回执不能释放新操作或删除仍使用的缓存。

#### Scenario: 观察来源和预留的集群责任
- **WHEN** 运维查看双节点聚合健康
- **THEN** 报告缺少有效 owner 的来源数量、活动 owner 自最近续租的最长时间、过期 held 及已到对账时点的 reclaiming 数量；这些读数不授权释放物理占用。

#### Scenario: 集群拓扑观察迟到与 DLQ 消息重投
- **WHEN** A 已获得数据库发放的 30 秒观察代次，B 在更新代次完成后 A 才返回；或 DLQ 在事务提交后、Broker ACK 前中断
- **THEN** Broker I/O 在事务外进行，过时代次的成功或失效结果不能覆盖新代次，两节点独立活性探测继续；DLQ 以消息携带的发布尝试和实际投递/死亡事实区分事件，同一事实幂等结算，无法确认新事件或有效新租约时保留既有责任。

#### Scenario: 持锁节点崩溃
- **WHEN** A在上述职责的领取或提交窗口退出
- **THEN** B按对应锁/租约恢复责任；A恢复后不能凭旧owner继续有效写入，不重复产生业务责任或释放容量

#### Scenario: 本地完成通知丢失
- **WHEN** Attempt在A终态但T2/投影运行于B，进程内通知没有送达
- **THEN** 持久扫描继续推进，在既定负载/预算内收敛，不要求新增跨节点消息总线

### Requirement: 原生来源保持唯一有效 owner
Kafka、Kubernetes、RabbitMQ、RocketMQ SHALL 以数据库owner/generation/到期时间进行接管；旧owner不得新增责任或推进不安全checkpoint。来源确认 MUST 只对应已持久接管的安全前缀；租约失效不能视为旧SDK、连接、线程和本地缓冲已停止。

#### Scenario: 旧 owner 恢复并继续读源
- **WHEN** B接管后A恢复其SDK回调
- **THEN** A的新T1和checkpoint写入被拒绝并关闭旧连接，最多重复确认已提交安全位置；原生工作预算直到真实结束才归还

#### Scenario: 停用与接管竞争
- **WHEN** 管理员停用/reset来源与B获取lease同时发生
- **THEN** 原锁和generation决定唯一合法结果，不在停用后新增接收，不丢弃已接管责任或复用旧epoch

### Requirement: 各来源按自身恢复合同验收
来源恢复 SHALL 分别验证Kafka assignment/offset、RabbitMQ channel与ACK、RocketMQ receipt handle与确认、K8s resourceVersion及LIST/WATCH完整性；内部派发Broker HA不能替代外部来源恢复证据。来源不可用时 MUST 保留可解释等待/需同步事实。

#### Scenario: 原生来源提交与确认之间崩溃
- **WHEN** Kafka/RabbitMQ/RocketMQ的T1已提交而来源确认结果未知
- **THEN** 新owner从安全位置恢复，重投命中原责任，不提前确认未提交后继，也不把来源重连算作业务重试

#### Scenario: K8s 初始同步中断
- **WHEN** LIST/WATCH或同步完成链尚未完整时owner切换
- **THEN** 保留partial/失败及既有完成事实，按原合同重新同步，不向目标提供虚假的完整快照或清理资格
