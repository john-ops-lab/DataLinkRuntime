# HTTP 持久接收规格

## Purpose

定义显式启用的 HTTP 持久数据接收，使发送方能识别消息是否已经被可靠接管、查询处理结果并安全重试；同时保护稳定身份、首次输入、已接管责任、访问权限及现有 Webhook 的使用合同。

## Requirements

### Requirement: HTTP 持久接收与原 Webhook 明确区分
系统 SHALL 提供显式启用的 HTTP 持久来源与独立接收路径，新建时默认停用。原 Webhook 的 accepted/completed、execution_id、等待及幂等行为 MUST 保持，不得自动转成只返回接收记录的模式。

#### Scenario: 原 Webhook 客户端继续调用
- **WHEN** 原客户端请求既有 Webhook 路径并选择 accepted 或 completed
- **THEN** 沿用原执行创建和响应合同，不被迁移到新持久入口，不要求改用 receipt_id

#### Scenario: 保存新 HTTP 来源
- **WHEN** 用户保存 HTTP 来源配置或校验路由和凭据引用
- **THEN** 不自动开始接收、不执行业务；明确开启前的新请求不产生已接管责任

### Requirement: 接收成功具有持久身份和可查询状态
系统 MUST 在完整输入、稳定身份、处理绑定、责任和容量占用同事务提交后，才对首次接收返回 202 及 receipt_id。接收状态 SHALL 区分已接收、待处理、执行中和业务终态；接收成功 MUST NOT 表示业务成功。状态查询 SHALL 返回原责任的当前安全状态，未创建执行时 execution_id 可为空。

#### Scenario: 提交前或提交后响应丢失
- **WHEN** 进程在 T1 提交前终止，或提交后但响应前终止，发送方使用原 ID/原文重试
- **THEN** 前者仍需完成一次接管，后者返回原 receipt_id；恢复后无虚假接收、第二条初始责任或遗漏

#### Scenario: 接收完成但执行尚未创建
- **WHEN** 容量暂不允许 T2 创建执行
- **THEN** 状态查询显示等待处理和既有 receipt_id，原责任持久保留，不用成功或空白掩盖等待

### Requirement: 稳定请求身份与原始正文一致性
新入口 SHALL 要求 1–128 字节可打印 ASCII 的 Idempotency-Key，以订阅、来源 epoch 和该 key 共同确定身份，并按原始 UTF-8 正文 bytes 的 SHA-256 比较内容。在有限请求资源可用并通过当前认证及原文核验后，同身份同内容 MUST 返回原接收记录；不同内容 MUST 返回 409 event_identity_conflict 且保留首次输入。不同 ID 的相同正文 MUST 视为不同合法消息。持久配额余量不得成为既有记录读回的条件；有限请求资源门禁不得被解释为删除原身份或责任。

#### Scenario: 重投时额度已满或来源已停止
- **WHEN** 有限请求资源可用，当前认证有效的发送方对既有消息使用原 ID 和原文重试，而新接收开关关闭、持久 pending/history 配额已满、来源已停用或 Worker/Broker/接收 owner 暂不可用
- **THEN** 原文核验后以 200 读回同一 receipt_id 和当前状态，不重新占用接收/执行容量，不丢弃原责任；当前无效凭据仍被拒绝

#### Scenario: 重投遇前置在途资源暂满
- **WHEN** 重复 POST 到达，而原请求数、正文预留或原生工作名额已满，尚未完成认证及原文核验
- **THEN** 仍可按原资源门禁返回 429 ingress_capacity_exceeded 和 Retry-After: 1，不新增责任或删除原记录；发送方保留原 ID/原文有界重试，已知 receipt_id 时可使用仍受有限预算约束的原 GET 通道，不将所有 429 推断为确定从未接管，不新增 pool、无限队列或只凭 key 绕过核验

#### Scenario: 相同 ID 改用另一份正文
- **WHEN** 发送方复用 ID 但改变正文，包括改变序列化后的原始 bytes
- **THEN** 拒绝冲突，不覆盖原内容、配置或责任，不产生新的 HTTP 来源永久坏消息屏障；其他合法请求仍可接收

### Requirement: HTTP 接管固定处理配置并遵守运行锁
每条接管责任 MUST 绑定不可变 Revision、固定 Worker、凭据引用和运行/重试策略；等待、T2 和自动重试 SHALL 使用该绑定。启用或排空中的普通保存、节点/凭据引用修改、来源切换和删除 MUST 按现行运行锁拒绝；名称描述与已授权受限 repair 例外保持。凭据真值 MUST NOT 进入输入或快照。

#### Scenario: 旧责任等待期间尝试换代码和节点
- **WHEN** 有已接管待处理消息时用户普通保存新代码、修改 Worker 或凭据引用
- **THEN** 操作被明确拒绝，既有绑定不变；显式受限 repair 不覆盖后续 pending 或原失败记录

#### Scenario: 停止接收与新请求并发
- **WHEN** 停用与 T1 竞争
- **THEN** 请求仅能在停用生效前合法提交并成为待处理责任，或被拒绝且未接管；旧责任继续排空，不随停用删除

### Requirement: HTTP 入口在读入和解析前限制资源
系统 SHALL 在读正文前限制请求数和字节预留，拒绝超限而非无限排队。首期单正文 ≤64 KiB、最终 Envelope ≤256 KiB、JSON 深度 ≤32、节点总数 ≤4,096，并限制解析并发、总读取/空闲时间和进程内存；未知 Content-Length 同样适用。压缩 SHALL 明确拒绝；超时或取消不提前释放仍被原生工作占用的预算。

#### Scenario: 未知长度和缓慢正文
- **WHEN** 请求没有 Content-Length、持续慢速传输或超出单正文/结构边界
- **THEN** 有界读取在规定时间/字节/结构限制停止，未成功接管，不保留无限正文或线程，其他合法请求可继续

#### Scenario: 合并路径完整读取后只归还未用raw预留
- **WHEN** 原batchhint选择合并路径，正文已完整读取成功，尚未调用collector.add
- **THEN** SHALL 同步归还`64KiB-len(body)`的未用raw额度并更新本请求实际held，继续保留`len(body)+256KiB`；MUST NOT 用收集尺寸估计替代完整Envelope额度，也不改变读取前320KiB预留及原32请求/8MiB/8native上限
- **AND** 2KiB正文的31项共持有7998KiB，剩194KiB不足下一请求读取前的320KiB，原ASGI提示仍可在第32项读正文前拒绝；其他既有资源不足仍可更早拒绝，不承诺吞吐
- **AND** 64KiB正文没有raw差额，读取失败、非batchhint及GET保持原路径；hint变陈回退单条时保留同一完整Envelope额度，取消、关闭或submit失败按原所有权恰一次归还实际held，真实原生工作未结束前不得提前释放

#### Scenario: 内容无法完整存储
- **WHEN** 合法传输中的 JSON 值不符合当前无损存储合同
- **THEN** 明确拒绝且不返回接收成功，不截断或自动清洗后假装接收了原始消息

### Requirement: 拒绝和不明结果支持发送方安全重试
容量不足 SHALL 返回 429，临时平台不可用 SHALL 返回 503，均提供有界 Retry-After；停用的新请求 SHALL 返回 409 ingress_disabled。发送方 MUST 保留未确认消息和稳定 ID，按原文有界重试或查询。超时/断线 MUST NOT 被解释为确定未提交；平台开关、容量和配置故障 MUST NOT 记为消息损坏。

#### Scenario: 容量触限后重试
- **WHEN** 首次请求因资源不足被拒绝，发送方按 Retry-After 用原 ID/原文重试
- **THEN** 未接管数据由发送方保留；额度合法释放后可接管，不提前成功响应、不伪造拒绝请求的接管数

### Requirement: 外部查询权限与身份留存有界
接收/查询 SHALL 使用当前来源有效 token，只能访问该来源的安全接收状态；token MUST NOT 获得管理处置权限。路由标识 MUST 固定绑定原订阅及 epoch，reset 使用新路由，删除后不复用。默认原文留存 7 天、去重 30 天，并保护未完责任及恢复引用；窗口外 MUST NOT 承诺永久去重。客户端约定的重试期限 MUST 在公布窗口内。

#### Scenario: 猜测其他来源接收记录
- **WHEN** 调用方使用自己的 token 查询其他来源的 receipt_id，或使用已撤销 token
- **THEN** 拒绝且不泄露消息、代码、凭据、地址或其他来源状态

#### Scenario: 原文到期但责任未完成
- **WHEN** 已接管消息仍在排队、重试或形成未解决失败
- **THEN** 保留恢复必需数据；不因原文 TTL 或配额紧张删除责任

#### Scenario: reset 后旧请求到达
- **WHEN** 发送方仍向绑定旧 epoch 的路由重试，而该订阅已显式 reset 到新路由
- **THEN** 在当前合法认证及留存窗口内只读回原记录，未命中则返回 410 ingress_epoch_retired；不按新 epoch 再接管一次；订阅已删除时不继续开放外部查询

### Requirement: HTTP 接管合并不连带拒绝其他合法请求
HTTP 多条模式的收集 SHALL 在 async 层有界完成，不让原生线程等待其他请求。 每个收集组 SHALL 最多16条、额外收集不超过25ms，按正文长度的保守原文加Envelope估计累计不超过1MiB，满组、字节上界或期限先到即封组并进入有界原生就绪调度；超期新请求不得进入旧收集组。原生claim时 MAY 只从已有未过期同路由ready前缀重包为最多16条/估算1MiB的实际T1单元，不直接借未封组成员、不等待新项或重置原期限；机会性提前封组 SHALL 先通过原flush完整进入ready，再按原claim裁决。估计不解析正文或读DB，T1仍按实际完整尺寸强制原1MiB。原 32 请求/8 MiB、8 原生工作和 8 接收连接 MUST 保持，与 MQ 共享接收连接预算而非另加 pool。组内同 ID 同原文的首次/重复分别返回已提交的 202/200；错误 token、坏 JSON 或 409 冲突仅影响对应请求，不生成来源屏障。取消等待者不得取消共享工作或提前释放预算。

#### Scenario: 合法请求与错误请求同组
- **WHEN** 同组含合法请求、坏 JSON、已撤销 token 或相同 ID 不同原文
- **THEN** 分别执行当前鉴权、身份与容量判断，合法项提交后才响应，错误项返回原错误；较大请求超额不阻止仍能容纳的小请求，已有记录仍可安全读回

#### Scenario: 合并提交结果不明
- **WHEN** 数据库连接或提交结果未知
- **THEN** 不猜测新接管成功，受影响请求以 503/原 ID 重试核对；已存/组内身份不创建第二条初始责任

#### Scenario: 单个等待者取消
- **WHEN** 同组一个 HTTP 等待者取消而共享事务仍在执行
- **THEN** 仅该等待者失去响应，共享事务及预算保留至真正结束；其他等待者按实际已确认提交结果分别收到 200/202 或原确定错误，不因他人取消被连带改成 503

#### Scenario: 已知请求预算不足时提前拒绝
- **WHEN** 接收 POST 到达，既有 HTTP 子预算或来源父预算已不足以容纳下一请求的预留，或原生工作名额已满，尚不能区分首次与重复投递
- **THEN** 可在请求中间件及正文读取前返回与原路径一致的 429 ingress_capacity_exceeded 和 Retry-After: 1；只读快检不初始化资源、访问数据库或改变预算，不改变原 _request 已有的拒绝及持久幂等合同；状态查询和管理 API 保持原路径，查询仍有原有限预算，放行后仍执行原认证及权威预算检查

#### Scenario: 进程退出后停用被动 HTTP 来源
- **WHEN** HTTP 持久租约仍由已退出进程的旧 owner 占有，而管理员停用来源
- **THEN** 在原锁与 fence 下同事务使该被动租约失效并显示已停止，无须等本地不存在的 SDK 回调；原已接管责任继续排空，未结束请求工作及其独立预算不能提前释放，MQ 仍依原生关闭事实确认结束

### Requirement: HTTP 中间件优化保持可信入口边界
系统 SHALL 在将入口边界替换为直接ASGI调用时，保持token/account入口的信任边界、私有前缀的path/raw_path处理、裸前缀拒绝、既有双提交CSRF及public hook/ingress豁免。非HTTP scope SHALL 原样透传。

#### Scenario: 繁忙时等价拒绝且不改变责任
- **WHEN** 并发HTTP调用遇到既有请求或正文容量耗尽
- **THEN** 系统 MAY 在返回同样有界拒绝前避免不必要的中间件任务分配
- **AND** 已接受调用 SHALL 在底层工作实际结束前保持原预留所有权，包括取消和提交结果不明场景
- **AND** 优化 SHALL 保持FastAPI路由、来源认证、T1、提交完成后才返回成功的检查及全部既有预算，不增加许可或队列
- **AND** 模型测试 SHALL NOT 替代真实功能或容量验收。

### Requirement: 有界HTTP组按路由获得原生工作
显式business_batch的实际原生单元 SHALL 同public_id最多一个未结束native单元，同路由成员FIFO、跨可运行路由轮转，并共享原8 native/8接收连接。所有等待成员 MUST 持有原32请求/8MiB额度，不新增正文副本或无界路由/任务缓存；旧单条和GET路径 MUST 保持。

#### Scenario: 同路由繁忙时等候、到期与其他路由推进
- **WHEN** 封组后该路由尚有native组未结束或原全局native额度已满
- **THEN** 各成员在原额度内保留封组时起最多额外1秒的原就绪期限，不延长25ms收集窗口；原生claim时仅可转交已ready且未过期的同路由FIFO前缀，不直接拆借collector或已提交成员，也不等待新项；提前封组另按锁外准备场景，不能延长原ready期限
- **AND** 未提交native即到期者返回原429/Retry-After，不产生接管/配额或坏消息，其他可运行路由获得轮转机会
- **AND** 原有native释放包括GET/旧单条 SHALL 触发有限就绪提示，避免无新POST时挂起

#### Scenario: 同路由已有就绪前缀组成原生单元
- **WHEN** 头组9条、后组11条均已ready且未过期，同路由原native名额可用
- **THEN** MAY 取前16条组成一个实际T1单元，后4条仍保持原顺序、Future、预留与原deadline；估算字节先到上限则只取可容纳前缀，不越过头部挑项
- **AND** 移空的等待组只清元数据，不能归还已转交成员的预算；该原生单元只取得一次native名额，每项由原实际完成或已知未提交拒绝路径恰一次释放
- **AND** 关闭/到期不能提前释放已提交成员，单个等待者取消不连带取消共享单元；逐项鉴权、T1责任及提交后才返回成功的规则保持


#### Scenario: 同路由collector在潜在claim前整体提前封组
- **WHEN** 原轮转的首个可claim候选已有未过期ready，模拟全体已有ready前缀后尚有条数及字节余量、无剩余nonfit或partial ready尾挡头，且同路由collector已有尚未到期且首项可容纳的FIFO前缀
- **THEN** MAY 在原owner loop锁内只预览候选、释放ready锁后核当前owner/未关闭和非预留native提示，再用原flush将collector整个组提前封组、取消原timer，通过原dispatch/enqueue进入ready；不直接拆借未封组成员、不为新项等待
- **AND** 任一路由已有到期ready须先由原_take/finish处理，首候选已满或不适配不能换挑后面较空路由；原_take SHALL 重新按原RR/期限/一次native名额裁决，提示变陈不算已预留或提交，至多留下原预算内的有界ready工作，不增加claim次数或绕过其他路由

#### Scenario: 提前封组保留原期限与所有权
- **WHEN** collector整体提前seal后，实际原生单元只取其部分成员，或准备时遇到期、更早ready阻挡、关闭、活跃路由或额度提示不足
- **THEN** 已有ready与donor尾项的原deadline MUST 不变；新组从这次实际seal起最多1秒，余项保留同一deadline和原Future/请求/字节预留，不推迟到原25ms截止后重新计时
- **AND** 无合法机会不促封；到期ready先按原路径处置，collector原截止检查保持。关闭竞态经原enqueue拒绝时恰一次释放，已开始submit或真实native不得早释；单个取消不撤销共享组


#### Scenario: 受控prepare异常仍推进原就绪工作
- **WHEN** 内部prepare优化钩子在副作用前或已成功enqueue后抛出普通异常
- **THEN** 异常 MUST 不逸出原pump而跳过原_take推进或最终_arm_timer；原head与已在ready的原/新组按原规则各恰一次推进
- **AND** MUST 不自动重试flush、回填collector或提前释放可能已转交的预算；此合同不声称任意外部插件或pop至enqueue中间任意异常/OOM无损

#### Scenario: 等待者取消或关闭时保留实际责任
- **WHEN** 某等待者取消或Control关闭且存在就绪/已提交组
- **THEN** 单个等待者取消不撤销已交给collector的共享组；仅尚未调用submit的就绪组可因到期或关闭等已知拒绝归还原额度，提交调用已开始及实际native仍按原最终结果释放，不能取消共享事务或提前清active
- **AND** 关闭/定时器/submit拒绝及回调竞态 MUST 恰一次收尾；原鉴权、配置/lease/开关检查、T1绑定和提交未知语义不变

### Requirement: 共享接收池只在全部使用者真实结束后关闭
Control关闭 SHALL 保持原15秒有界的原生来源等待；关闭回执 MUST 区分真实结束和仍在关闭或未知。HTTP原生executor实际排空后，只有同锁下原生owners/probes/starting均空且stop-manager线程结束，才允许dispose并清除共享接收engine/factory；不能因HTTP已排空就推定全部来源结束。

#### Scenario: 原生来源关闭超时或未知
- **WHEN** 关闭期限已到，但原生责任仍活、关闭未确认或回执不是明确成功
- **THEN** 保留原engine、pool和factory身份供原工作使用，维持共享8连接上限；不先清globals、不新开另一池、不提前释放责任或增加无限等待

#### Scenario: 两类接收工作均确认结束
- **WHEN** 原生来源真实结束且HTTP原生executor已真正排空
- **THEN** 才可dispose和清除原共享接收factory；管理/恢复保留池及持久责任规则不变
