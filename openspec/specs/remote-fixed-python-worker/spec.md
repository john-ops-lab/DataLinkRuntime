# Remote Fixed Python Worker Specification

## Purpose

定义 Wave 1-A 专用新部署及 W1-B 已提交结果受控恢复的单 Control、单固定 Worker、标准库 Python 远程运行合同。该能力不替代既有默认 v3 部署，不声明 Pool、HA、旧部署迁移或异常历史自动恢复。

## Requirements

### Requirement: 专用部署必须先建立可信身份和有限资源
远程模式 MUST 默认关闭。启用时 Control MUST 拒绝旧 Worker 身份、非阶段来源的执行责任、不可确认的运行角色及第二个有效 Control 应用进程。部署 MUST 预建持久 journal 锚点，固定身份、信任声明、签名密钥及镜像，保留正常停启所用的原 runtime volume。Worker MUST 使用验证证书链和主机名的 HTTPS/AMQPS，不得依赖数据库权限、Control 业务共享目录或业务入站端口。

#### Scenario: 旧部署尝试直接打开开关
- **WHEN** 数据库存在旧身份或非阶段执行/Attempt 责任
- **THEN** 远程模式启动失败，原数据和默认 v3 操作路径保持可辨识，不自动迁移或清账

#### Scenario: 持久锚点缺失或资源条件不成立
- **WHEN** Worker 无法确认原目录、实际 cgroup 归属、有限硬资源或禁止 swap 条件
- **THEN** Worker 不进入可领取状态，不以新空目录替换原责任

### Requirement: 新任务与最终 Claim 都必须核对阶段能力
远程阶段 MUST 只接受固定到阶段身份的标准库 Python 普通手动/定时任务及 none/JSON 输入。第三方依赖、内置包、业务凭据、托管文件、其他语言和其他触发方式 MUST 在新责任接受/启用之前拒绝。最终 Claim MUST 重新核对冻结快照；不兼容的旧责任不得被改写、删除或误报为已完成。

#### Scenario: 不支持的新请求
- **WHEN** 新执行或定时启用包含不支持的能力
- **THEN** 返回 `worker_task_capability_unavailable`，不新增对应 Execution、Admission 或 Outbox 责任

#### Scenario: 准备领取时预算不足
- **WHEN** 协商后的 wire/解析资源无法容纳冻结任务
- **THEN** 在提交 Attempt 前明确等待，不增加 attempt_count；已发生或未知的提交不得解释成这一等待

### Requirement: 每次有效操作都必须绑定当前身份和提交证明
protocol 4 操作 MUST 验证独立运行凭据、稳定身份、instance/generation、当前实例证明及适用的 Attempt/fence/Token。候选提交 MUST 经独立的及时观察才能形成授权证明；事务或回复未知、旧证明、期限已过时不得扩大权限。重复 Claim 不得产生第二个有效 Attempt。普通操作可共享 Token 命名空间围栏，凭据签发/撤销 MUST 保持独占。

#### Scenario: COMMIT 或证明观察跨越原有效期限
- **WHEN** 实例或 Attempt 候选的提交/观察超过原保守期限
- **THEN** 不签发可继续执行的证明，保留可能已经提交的责任，不靠名义期限或迟到回复续活

#### Scenario: 凭据被管理员撤销
- **WHEN** 旧进程继续使用被撤销凭据提交心跳或业务写入
- **THEN** 服务端拒绝授权，Worker 停止旧业务，原 Attempt、Slot 和本地未知责任不得因此释放

### Requirement: 终态、物理清理和 Broker ACK 必须依次闭合
成功、可确认的业务失败及执行超时 MUST 沿真实受监督执行链提交结果。Worker MUST 确认进程/IO 停止、获得可核验结果回执、删除对应 cgroup/workspace、确认服务端清理结算、持久删除本地记录并归还原预算后，才能 ACK 业务投递。单独数据库终态、签名回执或队列瞬时零值 MUST NOT 代替完整清理/ACK 证明。

#### Scenario: 结果已提交但回执未知
- **WHEN** 服务端已经保存终态，而 Worker 无法确认结果回执
- **THEN** Worker 停止并保留原 journal、预算及未结责任，不 ACK、不创建第二个有效 Attempt；普通带残留重启仍阻断，只有满足下述 W1-B 完整前提的显式恢复可继续

#### Scenario: 重复清理结算
- **WHEN** 相同归属、intent 和合法证明再次提交清理结算
- **THEN** 原结算事实可幂等确认，不重复释放 Slot，也不修改其他 Attempt 的责任

### Requirement: 管理员排空与正常停启必须保留维护状态
管理员维护操作 MUST 绑定 `request_id` 和 `expected_version`。draining MUST 拒绝新 Claim，并允许既有合法工作完成结果及清理；责任归零后才进入 disabled。正常停止和同身份重启 MUST 复用原持久目录、等待旧实例期限、执行真实 preflight，且不得自行解除 disabled；再次运行需要管理员显式激活。

#### Scenario: 工作进行中排空
- **WHEN** 管理员对有未结 Attempt 的 Worker 发起 drain
- **THEN** 返回 draining 和未结数量，已有合法任务可完成，重复同一请求在归零后确认 disabled

#### Scenario: 正常重启后再次运行
- **WHEN** 已完成排空的 Worker 用同一配置正常重启
- **THEN** 新 instance/generation 完成 ready 后仍为 disabled，显式激活后才可领取下一项任务

### Requirement: 接收、解析和结果进程必须保持真实资源归属
Control/Worker 存活的父子进程、业务数据副本和 IO MUST 受有限 cgroup 硬限及禁止 swap 条件约束。解析/结果子进程 MUST 在接收业务材料前应用其地址空间限额；真实结束/回收不明时不得提前释放资源账目。Control 可在同一 Attempt 内保留同一个有限资源子进程完成 Claim、结果 stage 和 finalize，但 MUST 每次重新验证当前 Control/Worker/Attempt 权威，原 Claim 期限和资源保留期限不得成为新授权。不同任务不得共享该子进程。

#### Scenario: 超预算或子进程停止不明
- **WHEN** 接收、解析、执行或结果子进程超限/异常，或无法确认原进程与 IO 已结束
- **THEN** 保留对应预算和责任，停止新工作，不误报 Claim 前容量等待或清理成功

#### Scenario: 正常结算后复用预算
- **WHEN** 同一 Worker 进程的上一项任务已经完成结果、清理和本地释放
- **THEN** 原预算可用于下一项任务；峰值观测只说明样本行为，不作为所有 JSON 的内存上界

### Requirement: 已提交结果受控恢复必须保留原物理与预算责任
W1-B MUST 在业务放行前持久化版本化 prepared 检查点，冻结原 Dispatch、身份/instance/generation、Attempt/fence、receive/task 两份预算及计费版本、boot/原卷锚点和实际对象；真实监督链确认进程/IO 停止后、结果 finalize 前 MUST 持久化 stopped 检查点，绑定 result intent/MAC，并不可逆禁止原业务再次启动。旧 W1-A 记录缺失此信息时 MUST 保持阻断，不得故障后补造资格。

#### Scenario: 旧 W1-A 残留缺少新检查点
- **WHEN** 重启扫描只能找到旧 journal 或缺失/损坏的 prepared/stopped 记录
- **THEN** 保持 recovery_required，不补造原预算或停止事实

### Requirement: 恢复必须绑定可信宿主操作及管理员意图
恢复 MUST 由管理员的版本化、幂等维护意图触发；绑定完整旧容器 ID、部署记录引用/摘要、原责任、boot 和 prepared 摘要。可信宿主操作者 MUST 在故障前建档并在故障后通过既有 Compose/Docker/Linux 命令确认整个旧容器和后代退出、原卷独占且无未知 FD/namespace 导出。Control MUST 将这些材料标为运维断言，MUST NOT 宣称 HTTP 参数或证据摘要自动证明物理停止。

#### Scenario: 无法确认原容器和独占卷前提
- **WHEN** 缺少故障前映射或不能确认整个旧容器/后代退出及无未知共享引用
- **THEN** 保持部署阻断，证据摘要和 HTTP 参数不能代替真实核验，不通过换空卷继续

### Requirement: 恢复实例必须独立核验历史责任并可安全重入
Worker MUST 独立核验其可观察的原锁/锚点、相同 boot/存储/身份、检查点和实际对象/挂载；新 namespace 观察不得覆盖旧绑定。宿主重启、复制卷、跨宿主接管和未知结果仍延期。

#### Scenario: 显式恢复具有完整资格的已提交结果
- **WHEN** 当前有效凭据读回匹配管理员意图、旧实例及候选期限已过，并且原卷检查通过
- **THEN** 只注册新的 reconciling 实例，不开 consumer、不报告 ready；用当前证明、原 Cleanup Token、两代历史和新 nonce 只读核对已提交 result intent/MAC/finalization，不使用旧证明或重发结果正文

#### Scenario: 清理或本地多文件删除中断
- **WHEN** 原对象的清理、cleanup 提交/回执或 journal/runtime 删除中断
- **THEN** 每项破坏操作前持久保存绑定原对象的删除意图，缺失对象只沿匹配意图重入；同名替换、嵌套挂载、损坏/额外对象仍阻断；已提交 completion 只读确认，未提交意图才可在当前围栏下重绑定；终结记录保存原 Cleanup Token、Dispatch/两份冻结额度、稳定结果/结算 ID 及精确记录摘要，逐项 unlink/fsync 后最后删除

#### Scenario: 恢复完成后重新调度与旧消息确认
- **WHEN** 物理清理、服务端结算、本地记录及实际管理 IO 已全部结束
- **THEN** 不对旧内存句柄 release 或将旧额度加回新账；重新验证 envelope/preflight 后建立一次新预算，保持维护状态并等待显式 activate；新 channel 对重投消息取得权威 ACK_NOOP 后才 ACK 当前 tag，再执行新任务，原 Execution/Attempt/业务结果不重建或修改
