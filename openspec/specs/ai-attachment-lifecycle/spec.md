# ai-attachment-lifecycle Specification

## Purpose

定义 AI 调试附件的独立临时资源生命周期，使原件、派生文件和转换活动只属于被授权的当前账号与 Adapter 范围，在重试、取消、页面范围变化和超时情况下保持身份稳定、资源有界且可以真实释放。

## Requirements

### Requirement: 临时资源范围所有权
系统 SHALL 为原件、派生表示、转换任务及任何上游文件引用绑定认证 Principal、Adapter 与临时页面范围；所有读取、引用和释放 MUST 验证当前 edit 权限及完整所有权，不接受仅凭文件引用跨范围使用。

#### Scenario: 相同账号尝试跨 Adapter 引用
- **WHEN** 当前 Adapter 的请求携带另一个 Adapter 的附件引用
- **THEN** 系统拒绝访问，不向模型发送该附件，不返回存在性或内部路径

#### Scenario: 账号范围变化
- **WHEN** 浏览器更换账号或临时页面范围
- **THEN** 新范围不能读取旧范围附件，旧异步结果不能提交到新范围

### Requirement: 活动引用与有限保留
系统 SHALL 为临时资源配置有限 TTL 与存储总量/数量上限，活动 Assist 和合法冻结轮次 SHALL 持有资源引用；清理不得删除仍被活动请求使用的资源，释放最后引用后资源必须在限定时间内回收。

#### Scenario: 清理与读取并发
- **WHEN** TTL 清理触发时某附件仍被合法 Assist 持有
- **THEN** 该读取正常完成，最后引用释放后按 TTL/释放合同回收，存储容量不被重复计费或无限保留

### Requirement: 临时聊天边界不变
系统 MUST NOT 把附件正文、提取内容、文件名、Prompt、Provider reasoning或原始响应写入业务数据库、InputRevision、Execution、持久聊天或知识库。仅为跨节点授权/回收，SHALL 允许独立TTL技术元数据保存opaque引用、范围身份、size/hash、内部随机对象引用、状态/期限/pin/任务generation及容量账；不得包含上述内容。临时原件/派生/名称sidecar必须位于独立受限区域，日志及审计不记录内容。

#### Scenario: 刷新与范围离开
- **WHEN** 页面刷新、重新挂载或离开账号/Adapter 范围
- **THEN** 聊天和 Candidate 不恢复，浏览器释放可释放临时资源，遗漏通知由服务端 TTL 回收

### Requirement: 冻结轮次身份一致
Retry 和 Regenerate SHALL 重用冻结请求的原件引用、大小与 SHA-256；资源过期或哈希不匹配时 SHALL 返回明确恢复错误，不得默默替换成当前新附件或其他材料。

#### Scenario: 重试前另选附件
- **WHEN** 用户在失败轮次后选择新附件，再重试旧轮次
- **THEN** 重试使用旧冻结附件身份，新附件只能进入新轮次，过期的旧附件提示重新附加

### Requirement: 真正终止转换工作
系统 SHALL 在取消、解析期限或进程预算超限后终止相应转换活动并回收其原件和派生临时目录；停止 HTTP 等待或线程 join 超时 MUST NOT 被视为转换停止。

#### Scenario: 转换器超时或客户端取消
- **WHEN** 隔离转换进程超过期限或所属范围被取消
- **THEN** 活动进程及子进程实际停止，临时文件释放，后续正常附件请求可成功，不污染会话

#### Scenario: 旧 inline 兼容入口解析超时
- **WHEN** 不携带 prepared refs 的合法 inline PDF、Office 或文本请求解析超过原期限
- **THEN** 解析在有界隔离进程执行，返回既有超时错误前进程已终止并回收，许可归还；不留下继续运行的解析线程，后续合法文件可恢复

### Requirement: CPU 与临时存储有界
系统 SHALL 在 CPU 单机部署中限制并发转换、排队、进程、内存、页/图像数量、像素与派生字节；任何边界触发必须给出稳定阶段错误与已完成覆盖，不得在失败后留下无界后台工作。

#### Scenario: 多个扫描件并发
- **WHEN** 请求数量超过配置的转换容量或单文件派生内容超过预算
- **THEN** 超额请求被有界排队或明确拒绝，正常资源额度不被突破，已生成内容按所有权回收

### Requirement: 临时附件在任意 Control 保持同一授权和身份
模式C SHALL 以共享临时内容和TTL技术元数据支持A上传、B读取/解析/Assist；每次引用仍验证当前Principal、Adapter、scope及edit权限，保留原size/hash和冻结轮次引用。进程内索引或共享目录的单owner.lock MUST 不再是唯一权威，不要求sticky session。

#### Scenario: 上传后切换 Control
- **WHEN** 附件在A准备后，同scope合法Assist或Retry落到B
- **THEN** B读取同一受授权原件/派生内容，不能把缺少本机索引当作新上传，也不替换冻结附件

#### Scenario: 不同用户或 Adapter 使用引用
- **WHEN** B收到另一Principal/Adapter/scope的引用
- **THEN** 拒绝访问且不发送给Provider，不泄露其存在性或内部路径

### Requirement: scope 关闭和过期不能因节点切换复活
scope关闭/到期 SHALL 形成覆盖有效引用窗口的TTL关闭事实，所有节点重新读取权威状态；旧解析回调、缓存或重复请求 MUST 不能复活引用。页面刷新/离开仍清空临时聊天和Candidate，不增加历史恢复。

#### Scenario: A 关闭范围 B 收到迟到任务
- **WHEN** A已关闭scope而B的旧解析/Assist回调随后提交
- **THEN** 拒绝发布/引用，按原生命周期回收，不能重建聊天或把旧内容并入新scope

#### Scenario: 数据库恢复出旧临时元数据
- **WHEN** 数据库从备份恢复了旧scope、引用、pin或任务记录
- **THEN** 恢复流程在开放附件功能前使这些引用失效并治理临时内容，不能重置绝对到期时间、复活已关闭范围或把技术元数据当作可恢复聊天

### Requirement: 解析接管与物理终止分别验证
跨节点解析 SHALL 有lease和单调generation，只有有效generation可发布派生结果；取消/过期须实际终止对应进程与子进程。旧工作仍存在时 MUST 保留原节点资源charge，不能因lease失效提前释放；新解析只能在另外真实可用的预算内开始。

#### Scenario: A 暂停而 B 接管解析
- **WHEN** A的解析lease失效但实际子进程状态未知
- **THEN** A旧结果不能发布，B仅在合法剩余预算内重建；A的资源保留待回收，恢复后清理，不把HTTP断连当作停止证明

### Requirement: 附件容量和 GC 具有集群边界
原临时TTL、数量、原件/派生字节上限 SHALL 延伸为同一部署的总额度，节点转换并发与汇总预算分别标明。GC须保护合法活动pin、关闭范围和未停止原生工作，实际内容删除及技术元数据结账 MUST 幂等；禁用附件或要求重传只能作为显式阶段降级。

#### Scenario: 两节点争抢最后附件额度
- **WHEN** A/B并发上传会共同超过部署上限
- **THEN** 仅合法预留成功，另一请求明确拒绝；不会各自获得完整512MiB默认额度

#### Scenario: 故障后删除与读取并发
- **WHEN** B恢复GC，另有合法冻结请求仍持有活动引用
- **THEN** 内容保持可读，最后引用释放且真实工作结束后按TTL回收，不重复收费/释放，也不永久保留失联元数据

#### Scenario: 已开始的 Assist 中断
- **WHEN** 服务端已经发起 Provider 请求，此时浏览器或 Control 连接断开
- **THEN** 客户端显示中断，不因切换 Control 自动重发。用户主动 Retry 时重新校验 scope、权限与冻结引用；过期时提示重新上传，页面刷新不恢复旧聊天。
