# Worker Input Protocol Specification

## Purpose

定义 Worker v1/v2 兼容、Execution 文件下载授权、三语言 Context 和可恢复 Workspace 清理合同，使文件运行在崩溃、断网和重试下仍安全收敛。

## Requirements

### Requirement: I下载由当前双证明授权且有原截止
W1-I SHALL 使用只读GET `/api/workers/{worker_id}/v4/executions/{execution_id}/input-artifacts/{artifact_id}/content`，同时核验TLS、运行凭据、当前instance/generation/proof、Attempt/fence/proof、Claim Token及冻结Artifact/活动Lease。MUST 拒绝prepare-only、旧/过期/跨节点或Attempt证明、cleanup-only、query Token、Range、重定向和压缩；无v3旁路，不暴露storage key/宿主路径，响应no-store。

#### Scenario: 普通续租、撤销或取消
- **WHEN** 同一实例/Attempt正常推进证明，或出现撤销/取消/归属失效
- **THEN** 已准入只读流保留原有限截止，普通续租不延长，旧证明不新开流；每个≤64KiB块间重检原身份/fence/Slot/Lease及撤销/取消，失效或不明停止新读取/发送，不宣称撤回已发字节

### Requirement: I文件IO复用原资源且校验后才启动
W1-I SHALL 复用同一已持有的K份Control Claim/Result child和Worker staging child，最多K流/每Attempt一流，顺序最多4次完整GET、无文件级重试或续传。MUST 限块≤64KiB、单侧最多两块在途，父进程不读取或哈希业务文件正文；fd/IPC/内存/pids/nofile/tmp及原件/part/发布输入/Sandbox并存副本计入真实预算，不能另抢第二槽。DB事务结束后才进行存储/网络IO；Worker沿原Attempt→实例会话锁序只串行到有界准入，不持续租锁读正文。

#### Scenario: 有界准备、只读发布与失败
- **WHEN** 已合法finalize/start的任务准备文件
- **THEN** 使用Claim finalize前冻结的5秒准备上限、原执行/实例/Attempt截止和每文件开始+5秒的最早者，不从staging重算或累加20秒；Control核验同fd原件，Worker独立核对size/hash和对象身份，全部内容/manifest校验同步后发布0444/0555 input并最后发布manifest，业务启动门直至成功才打开；权限仅防误写，同UID不作强只读承诺，Sandbox得到独立副本

### Requirement: H业务凭据只沿冻结引用与合法Claim交付
W1-H SHALL 只为原无依赖Python/JavaScript、manual/schedule、none/JSON、strict fixed/Slot 0及K=1/2任务增加现有password/token/access_key/secret的绑定字段；引用在接受时冻结，值在合法Claim流程按原Credential ID解密。MUST 同时满足当前目标的H处理能力和绑定稳定 Worker ID/identity 与 Credential ID 的冻结私有部署许可，接收、schedule启用/触发、派发及两阶段Claim全部重检；不改用当前Adapter绑定，不向Broker发送secrets或完整TaskPayload，不新增公开明文查询。

#### Scenario: 未许可的新任务或旧节点
- **WHEN** 节点仅有Python/JS能力但未开启H能力，或本次冻结Credential ID未获该稳定身份许可
- **THEN** 新请求在创建Execution/Admission/Outbox前明确拒绝，已接受责任保持原路由并有界暂停；不得转发给另一节点、增加Attempt或从自报能力推导许可

#### Scenario: 引用不变而真值在领取期间更新
- **WHEN** 当前Adapter绑定已替换，或同一Credential ID的值在prepare与finalize之间改变
- **THEN** 继续按原不可变引用校验；两阶段在原锁前缀后按Credential ID升序锁定并核对精确wire，不相同则回滚候选、零有效新Attempt；已提交Attempt使用已下发值，不重新解密净化结果，后续合法Retry可按原引用取得新值

#### Scenario: tentative响应与提交未知
- **WHEN** 合法目标已收到有界prepare payload，但final Claim/当前证明未被确认，或提交可能成功而响应丢失
- **THEN** tentative真值仅处于已授权目标的受限Claim child内，不授权业务启动；沿原未知责任和物理对账合同保留，不重建Attempt或提前ACK

### Requirement: Claim准备在原时限内保持当前实例证明
Worker MUST 在复制当前实例证明至原Claim child完整接收tentative回复期间，与同进程实例续租互斥，避免续租提交新candidate后继续发送旧证明；MUST 保持原本机期限、最多5秒传输预算及stop/失权信号，不续期或放宽Control验证。该区间结束或异常时释放原锁，再进行资源/journal及finalize等需要重新进入lease的操作；未确认child/IO回收仍保留原责任和计费，不能凭互斥伪造Claim、结果或ACK。

#### Scenario: 复制证明后实例续租开始
- **WHEN** Claim已复制当前证明，续租线程在child交接或tentative接收期间请求更新candidate
- **THEN** 续租等待这一次有界交换结束；prepare只发送当时仍为当前版本的证明，随后沿原续租及最终Claim证明流程继续，不开放旧证明兼容窗口

#### Scenario: 准备期间到期或失权
- **WHEN** 原准备期限到达、stop/失权信号触发或交换失败
- **THEN** 在原预算内失败并释放互斥；只有原child/IO确认回收后才释放相应预算，未知责任不自动重试业务或ACK，不延长期限以等待续租

### Requirement: H凭据注入有固定上限且不继承宿主secret
W1-H SHALL 遵守以下固定上限：最多16绑定、env_key为64字符内的固定环境名，单值4096/总值16384 UTF-8 bytes且无NUL，ciphertext单项32768/不同凭据合计65536 bytes；先在DB检查计数/长度，再在原有界Claim child解密与验证。Worker MUST 校验映射与冻结绑定完全匹配，复核原全字段HMAC，仅将本次值加入封闭sealed launch的DLR_SECRET_*；原65536-byte launch、Claim/结果硬预算及standard资源限额保持，不扩大任意env或写普通凭据文件。

#### Scenario: 超大值、密文或编码膨胀
- **WHEN** 绑定/ciphertext/解密值超限，字段缺失、Master Key不可用，或精确环境/TaskPayload编码超过原预算
- **THEN** fail closed，不在Control父进程先解密超大对象；确定Claim前不生成有效Attempt，已可能提交则保持unknown，不以固定UTF-8限额冒充完整内存上界

#### Scenario: Agent环境或映射被污染
- **WHEN** 宿主存在同名DLR_SECRET_*、NODE_OPTIONS/NODE_PATH，或TaskPayload多出/缺少/修改绑定值
- **THEN** 只使用本次规范映射与原固定启动参数；不合并宿主secret，不经shell，指纹/归属/字段异常在业务启动前拒绝并保留相应已Claim责任

### Requirement: H结果脱敏使用原Attempt的易失封印材料
W1-H SHALL 在launch交接前保留同一封印内核对象的有限opaque duplicate，绑定原Attempt及fd身份/长度/seals并计入原task预算，只交给既有结果child读取本次环境；父进程不解析新增secret映射，原有界原始输出缓冲不能被打印、落普通诊断文件或外发。MUST 在结果child中对完整流和JSON字符串键值进行本次值的精确字面量脱敏，净化后才编码preview/size/结果MAC；截断流和截断/非法output不得返回原始片段。无凭据路径、原结果语义、B/C版本及cleanup/结账/ACK条件保持，不承诺恶意变换外泄防护或宿主root隔离。

#### Scenario: 完整输出跨读取块或包含秘密的JSON键值
- **WHEN** 业务将高熵canary分块写入stdout/合并stderr，或写入完整JSON字符串键和值
- **THEN** 净化后的全部可读结果不含原值，JSON遍历及长值优先替换在preview/截断和MAC之前完成；先前分块不能绕过最终完整有界数据检查

#### Scenario: JSON键在脱敏后碰撞
- **WHEN** 两个原字符串键在字面量净化后成为同一键
- **THEN** 不静默覆盖，整份output返回null/空preview并标记output_redaction_conflict，按实际净化body重算size/MAC，原成功终态保持且不触发业务Retry

#### Scenario: 原始流或output已经截断
- **WHEN** 凭据任务的原流被ring截断，或output因超限/非法而无法完整解析
- **THEN** 对应原始片段不外发为preview，使用固定非敏感省略标记/空preview并保留原truncated、错误和业务终态；不凭匹配不到完整秘密断言片段安全

#### Scenario: 脱敏fd缺失、被替换或跨Attempt
- **WHEN** 结果child不能验证同一sealed对象，或最后读者/IO停止不明
- **THEN** 不提交未净化结果，不把业务退出当作完整cleanup/预算释放/ACK；跟踪所有fd引用及原责任，不能以重读最新Credential替代失去的本次值

#### Scenario: 重启后原易失材料消失
- **WHEN** 原任务停止后重启，只有既有持久检查点而无本次脱敏fd
- **THEN** 只在原B/C资格内观察已存在的净化结果或清理，不重建/补提交业务结果，不往journal补存secret，不扩大无stopped或取消失权的恢复窗口

### Requirement: Worker 注册显式协商协议版本
Worker 注册 SHALL 携带 `protocol_version`；缺失或显式 JSON `null` 按 v1 兼容，支持 Claim/Cleanup Token 与持久清理日志的 Worker 使用 v2。TaskPayload 中非空版本 MUST 是整数 `1` 或 `2`，不得把 bool、float 或数字字符串强制转换为协议版本。Control MUST 保存并在调度判断中使用该版本。

#### Scenario: 旧 Worker 注册
- **WHEN** Worker 注册未携带 protocol_version
- **THEN** Control 将其视为 v1，并只允许领取符合最低协议的 none/json Execution

#### Scenario: v1 领取文件 Execution
- **WHEN** v1 Worker 尝试领取 managed_files Execution
- **THEN** Control 返回 `worker_protocol_incompatible` 且 Execution 保持可由合格 v2 Worker 领取

#### Scenario: 非整数协议版本
- **WHEN** TaskPayload 携带 bool、float、数字字符串或未知整数协议版本
- **THEN** Worker 在 journal、Workspace、依赖准备和 Adapter 进程副作用前返回 `worker_protocol_payload_invalid`

### Requirement: v2 Claim 签发分权 Token
v2 Worker 原子 claim 时 SHALL 获得至少 256-bit CSPRNG 的 Claim Token 和独立 Cleanup Token；Control 只保存各自单向哈希并使用 constant-time comparison。两类 Token MUST 不可互换。

#### Scenario: Progress 使用 Cleanup Token
- **WHEN** Worker 对 progress/result/download 携带 Cleanup Token
- **THEN** Control 返回 `execution_claim_token_invalid`

#### Scenario: 清理回执使用 Claim Token
- **WHEN** Worker 对 cleanup receipt 携带 Claim Token
- **THEN** Control 返回 `execution_cleanup_token_invalid`

#### Scenario: v1 兼容 Execution
- **WHEN** v1 Worker 领取 none/json Execution
- **THEN** Control 不生成 Token，并在终态记录 `deferred/workspace_cleanup_legacy_unverified`

### Requirement: Worker 下载由 Execution、Lease 与 Claim Token 联合授权
内部 Artifact 下载 SHALL 校验 Worker 拥有该 running Execution、Claim Token 有效、Artifact 位于活动 Lease、Artifact 可读且元数据与内容一致；下载 API MUST 不接受浏览器或普通管理员会话替代 Worker 授权。

#### Scenario: Lease 外 Artifact
- **WHEN** 拥有 Execution 的 Worker 猜测同 Adapter 但不在本次 Lease 的 Artifact ID
- **THEN** 下载被拒绝且不返回对象路径或存在性细节

#### Scenario: Token 在 URL
- **WHEN** 请求尝试把 Token 放在 URL/query 而非指定 Header
- **THEN** Control 不接受该凭据，且访问日志不出现 Token

### Requirement: TaskPayload 不暴露存储路径
managed_files TaskPayload SHALL 只包含 Execution 文件项的 ID、ordinal、受控 mount name、展示元数据、大小和 SHA-256，以及 Execution 固化的清理超时；MUST 不包含 Control 宿主路径、storage key 或 ArtifactStore 根路径。受控 mount name SHALL 由 ordinal 与白名单扩展名生成，未知或非法扩展名退化为无扩展名安全名称。

#### Scenario: 领取文件任务
- **WHEN** v2 Worker claim managed_files Execution
- **THEN** 每个文件具有稳定 ordinal 与平台生成的 `input-<ordinal>.<controlled-extension>` 或安全无扩展名 mount name，原始文件名仅为元数据且不参与目录选择

### Requirement: Worker 在启动 Adapter 前完整准备输入
Worker SHALL 在受控 Execution Workspace 内下载全部 Lease 文件，逐项复核 size 与 SHA-256，写入不含敏感路径的 manifest，并将文件设为 `0444`、输入目录设为 `0555` 作为 best-effort 防误写；任一失败 MUST 不启动 Adapter 子进程。

#### Scenario: 下载中断
- **WHEN** 任一 Artifact 下载中断或返回非成功状态
- **THEN** Worker 不启动 Adapter，返回稳定下载错误并进入受控清理

#### Scenario: 校验不匹配
- **WHEN** 下载字节数或 SHA-256 与 TaskPayload 不一致
- **THEN** Worker 删除临时副本、不启动 Adapter并上报 `input_artifact_checksum_mismatch`

#### Scenario: 只读不是安全隔离
- **WHEN** 文档或 UI 解释输入目录权限
- **THEN** 只描述为同 OS 用户下的防误写措施，不宣称强安全只读边界

### Requirement: Workspace 创建前持久化私有清理日志
v2 Worker MUST 在创建 Workspace、下载文件或启动 Adapter 前，把 execution ID、受控计划路径和原始 Cleanup Token 原子写入 Workspace 外的私有 journal；目录权限为 `0700`、文件为 `0600`，写入使用同目录临时文件、fsync 与原子 rename。

#### Scenario: Journal 持久化失败
- **WHEN** Worker 无法安全持久化清理日志
- **THEN** Worker 不创建 Workspace、不下载文件、不启动 Adapter，并上报稳定错误

#### Scenario: Journal 内容审查
- **WHEN** 安全测试读取 journal schema
- **THEN** 只存在 cleanup 所需 execution/path/token，不包含 Claim Token、用户文件名、JSON input、业务 Secret 或 Adapter 输出

#### Scenario: 创建 Workspace 后立即崩溃
- **WHEN** journal 已持久化且 Worker 在 Workspace mkdir 后、完整下载 manifest 写入前崩溃
- **THEN** Workspace 已具有最小归属 marker/manifest，重启扫描可由 journal、受控名称与最小归属事实证明并安全清理，不留下无法收敛的空目录

### Requirement: 同步 Workspace 清理具有硬预算
所有业务终态 SHALL 尝试删除整个 Execution Workspace；单次尝试与总阶段分别受 TaskPayload 快照限制，总预算包含全部尝试、退避和本地确认。达到预算后 MUST 立即以 `deferred` 上报，不得无限阻塞结果。

#### Scenario: 删除文件系统调用挂起
- **WHEN** 单次删除超过 attempt timeout
- **THEN** Worker 中断本次尝试并在总预算内重试或上报 deferred

#### Scenario: 清理失败不覆盖业务成功
- **WHEN** Adapter 输出成功但 Workspace 删除失败
- **THEN** Result 仍为 succeeded，并附 `workspace_cleanup_status=deferred`、`workspace_cleanup_error_code=workspace_cleanup_failed`

### Requirement: Cleanup Receipt 独立且幂等
Worker SHALL 通过 canonical `/api/workers/executions/{execution_id}/workspace-cleanup`、仅接受 Cleanup Token 的独立端点上报 Workspace 清理；端点只允许业务终态的 `deferred→completed` 与 `completed→completed`，MUST 不修改业务状态、output、业务 error、ended_at 或触发重跑。若保留旧路径兼容别名，文档与 Worker client MUST 只使用 canonical 路径。

#### Scenario: 非法 v2 payload
- **WHEN** v2 TaskPayload 缺少 Token、超时不变量、受控 mount name 或其他必需字段
- **THEN** Worker 在创建 journal、Workspace或 Adapter 进程前拒绝 payload并返回稳定协议错误

#### Scenario: v2 代码与依赖字段
- **WHEN** v2 TaskPayload 的 `language` 或 `code` 为空白，或 `requirements` 缺失/不是字符串
- **THEN** Worker 在任何本地副作用前返回 `worker_protocol_payload_invalid`；`requirements=""` 表示无外部依赖并保持合法

#### Scenario: Result 响应丢失但 Workspace 已删除
- **WHEN** Worker 重启后 journal 显示 Workspace 不存在且先前 Result 响应未知
- **THEN** Worker 幂等提交 completed receipt，Control 对 completed→completed 返回成功后 Worker 才删除 journal

#### Scenario: 非法清理状态转换
- **WHEN** Worker 对非终态 Execution 或不允许的状态提交 cleanup completed
- **THEN** Control 返回 `workspace_cleanup_transition_invalid` 且业务事实不变

### Requirement: Worker 重启与周期扫描安全治理孤儿 Workspace
Worker 启动和运行期间 SHALL 扫描 journal 与受控 Workspace；只有同时匹配受控名称、归属标记和 manifest 的 Execution 目录可删除，MUST 不按宽泛年龄/路径删除版本依赖缓存或未知目录。

#### Scenario: Worker 崩溃后恢复
- **WHEN** Worker 在下载、执行或清理阶段崩溃后重启
- **THEN** 它从 Workspace 外 journal 恢复清理，删除遗留副本并幂等回执

#### Scenario: 未知相似目录
- **WHEN** runtime root 中存在名称类似但缺少合法标记或 manifest 的目录
- **THEN** 孤儿扫描保留目录并记录脱敏告警

### Requirement: 三语言 Context 提供稳定文件 API
Python `context.input_files`、JavaScript `context.inputFiles`、Java `context.inputFiles` SHALL 按 ordinal 返回只读元数据对象，至少包含 path、original name、content type、size bytes、SHA-256；JSON `input` 合同继续原样传递。

#### Scenario: Python 读取文件
- **WHEN** Python Adapter 运行 managed_files Execution
- **THEN** `context.input_files` 返回当前 Workspace 的受控路径和快照元数据，`input` 参数为 null

#### Scenario: JavaScript 与 Java 同构合同
- **WHEN** 同一文件输入分别由 JavaScript 和 Java Adapter 执行
- **THEN** 两种语言的 `context.inputFiles` 提供等价顺序与元数据，且不暴露 Control 路径

#### Scenario: JSON Execution
- **WHEN** source 为 json
- **THEN** `handle(context, input)` 继续接收原始 JSON，文件集合为空且不使用包装对象

### Requirement: Token、路径与文件内容不得进入日志
Claim Token MUST 不落盘、不持久化数据库原文、不进入 URL、普通/审计日志、Execution 历史或浏览器；Cleanup Token 只可存在 Worker 私有 journal，不得进入其他日志。错误与审计 SHALL 使用稳定 code 和非敏感 ID。

#### Scenario: 故障注入日志扫描
- **WHEN** 测试注入下载 401、Result 超时、清理失败和 Worker 崩溃
- **THEN** Control/Worker/Web 日志、审计和 API 响应均不包含原始 Token、storage key、宿主路径或用户文件内容
