# Remote Fixed Worker Specification

## Purpose

定义 W1-A 至 W1-F 的专用身份、受限恢复/结清、运维、双固定节点与停机换镜像，以及 W1-G 无第三方依赖 JavaScript、历史 F→G 和 W1-H 有界业务凭据/G→H，以及W1-I受限文件输入/H→I 及W1-J有界HTTP运行凭据/I→J的单 Control 远程运行合同。默认 Python 和一个 Worker，显式双语言及两槽时支持相应能力与两个各自单并发的固定 Worker。原规格路径保持。该能力不替代既有默认 v3 部署，不声明 Pool、HA、任意升级链或异常历史自动恢复。

0090 扩展固定执行的模式 A 迁移窗口、语言能力、固定触发、进度与维护意图。以下各历史阶段的限制按其来源版本解释。

## Requirements

### Requirement: 模式 A 的共享认证迁移窗口绑定原身份和数据库截止
单槽固定模式 A 的离线 owner SHALL 仅在已有合法未迁 v3 身份时打开一次窗口，截止时间 MUST 在权威数据库当前时间之后且不超过七天。fresh 默认关闭；关闭或到期后不能重开。其他专用部署模式不得取得该窗口。

#### Scenario: 管理员原地采用旧 Worker
- **WHEN** 管理员提交与原 Worker 匹配的采用请求，提供可信停机证据摘要，且控制面核实原身份和未结责任
- **THEN** 仅在满足停用与结清条件时沿原 ID/UUID 登记身份并返回含请求 ID、原 Worker ID/UUID 和名称的采用回执，保留 Adapter 绑定。确认回执后，管理员另行调用现有 enrollment 签发接口取得一次性凭据；已迁身份拒绝旧共享 Token。有 queued 等未结责任时拒绝采用，保留原数据；摘要用于关联操作者的现场记录。

### Requirement: 固定 Worker 仅报告经预检的语言组合
Worker SHALL 默认报告 Python，显式配置可选择 Python/JavaScript、再加 TypeScript/Java、或再加 Go 的封闭组合。当前实例 MUST 在原隔离和资源配置下通过所选工具链预检后报告能力；接账和 Claim 均须检查目标 Worker 的实际能力及输入条件。

#### Scenario: Go 编译并执行同一 Attempt
- **WHEN** 有效固定目标通过五语言预检，收到符合支持条件的 Go 任务
- **THEN** 构建和运行使用各自 helper、同一 Attempt 及原执行截止；构建子进程回收后才可在当前授权下运行已核验的只读制品，共享 workspace 保持 noexec；业务结束后仍按原清理及 ACK 顺序结账。

### Requirement: 固定触发与进度沿用不可变任务归属
固定目标 SHALL 继续支持原 manual/schedule；固定 webhook 接受对应 webhook Adapter 的 JSON，固定订阅接受对应 task Adapter 的 JSON。MUST 沿用原接账、快照及固定 Worker 的 Claim 门禁。运行中进度须绑定当前凭据、实例证明、Attempt 证明和 fence。

#### Scenario: 上报运行进度
- **WHEN** 原 Attempt 正在运行且授权有效
- **THEN** 在相同归属下更新进度；已取消请求不追加输出。带业务凭据的任务只允许状态进度，拒绝通过进度接口发送 stdout/stderr 文本。

### Requirement: standalone 的停机升级与强停保留责任
J 到 0090 的发布对 SHALL 沿既有独占停写、原库、原持久卷与 owner 权威转换流程升级，保留稳定身份、可信配置与升级回执。管理员强停 MUST 核对请求标识、维护版本、当前 drain 意图及活动 Attempt 归属，保留仍待结清的责任。

#### Scenario: 已排空中的节点收到强停意图
- **WHEN** 管理员对 draining 节点提交有效强停请求且活动 Attempt 的身份归属一致
- **THEN** 记录操作，将调度置 disabled、容量置 reconciling，返回未结 Attempt 数；该操作回执不得作为物理清理、释放 Slot/Lease 或 Broker ACK 的依据。

### Requirement: 固定 Python Worker 须显式启用受限 builtin wheel
Worker SHALL 默认关闭 builtin wheel。仅在已启用 managed-files、显式选择 pure_python 且原生预检通过时报告该能力。无业务凭据的固定 Python none/JSON 任务可使用一个精确 name==version 的 py3-none-any wheel，无传递依赖。Control MUST 冻结文件版本、大小和摘要，将下载绑定当前实例、Attempt/fence 和原截止。

#### Scenario: 发布并结清一份冻结材料
- **WHEN** 任务符合受限合同，当前证明和冻结材料一致
- **THEN** Worker 核对完整文件摘要，经受控输入名在 noexec 工作区内有界解包；保持同一 Attempt、本地清理与自身 ACK 的原有顺序。

### Requirement: 固定 Worker 缓存维护限于受控根和有限操作
Worker SHALL 在显式配置的独立 0700 私有缓存根上核对集群、稳定身份、store ID 与根对象，保留活动引用及历史保护。managed_cache 节点仅接受绑定当前实例和有效授权的 preview 或 clean，每轮最多八个键。快照自报不得独立授权删除。

#### Scenario: 维护授权过期或根归属不符
- **WHEN** 命令或 guard 失效，或者已记录的根归属不能核实
- **THEN** 拒绝相应处置，保留未结责任；确认清理需匹配原操作与本地物理回执。

### Requirement: 原生固定 Worker 的 Adapter 清理须先归档
具有原生 ephemeral profile 的固定 Worker SHALL 只领取自己已归档 Adapter 的精确清理责任，按当前签名证明核对 Worker、Adapter、cleanup ID 和领取次数。归档和领取时须保留清理证据。

#### Scenario: 已归档 Adapter 尚无匹配完成回执
- **WHEN** 已创建或领取清理请求，Worker 尚未完成独立物理核对并提交匹配回执
- **THEN** 保留未结责任，不因归档或领取成功提前宣布清理完成。

#### Scenario: managed_cache 节点处置已归档 Adapter
- **WHEN** 原生节点持有该 Adapter 的有效 cleanup ID、Adapter ID 和 claim 次数，并核对本节点专用缓存根的身份
- **THEN** MUST 在当前实例授权下以该票据取得对象级 guard；仅处置与观察身份匹配的目录，完成物理核对后再提交归属回执。票据、目录或 guard 已变时保留未结责任。

### Requirement: 恢复已暂存的结果必须重新取得签名观察
恢复读取 SHALL 仅识别与原 prepared/stopped、Attempt/fence、结果意图及本地暂存记录精确匹配的后继；正式发布前的中断只能按该唯一合法分支核对。读取旧记录成功不得授权状态变更。

#### Scenario: 新实例验证发布前暂存的历史后继
- **WHEN** 新实例在原锚点发现已暂存的结果 journal，而对应 stopped 后继尚未发布
- **THEN** 先只读核对原对象与结果记录，再取得当前代际签名观察并验证其绑定、nonce 和有效期限，才可进行恢复变更。旧证明、歧义后继及绑定不一致均拒绝，保留历史。

### Requirement: 在用 Adapter 的执行历史须在本地 journal 退役确认后才能进入常规保留清理
Worker SHALL 先用有效实例证明读取同一实例/代际已提交的终态清理回执，随后核对原物理根已静默，只确认相同 Execution、Attempt、fence 和 completion ID。Control MUST 持锁核对该绑定，提交后在独立事务重验当前权威并读回。运行历史的天数与每 Adapter 条数策略须保护全部尚未确认退役的 v4 Attempt，并继续核对其他已有保留条件。

#### Scenario: 在线 Adapter 超过历史保留上限
- **WHEN** 终态执行超过适用 TTL 或每 Adapter 行数上限
- **THEN** 仅在该执行全部 v4 Attempt 都已确认 journal 退役、原有订阅/幂等/Outbox 等保护全部解除时，才可通过常规保留策略删除。原代际不匹配、回执不明或仍有本地对象时保留，等待受控清理；独立读回失败不能作为退役确认。

### Requirement: J管理员HTTP凭据管理不扩大其他授权
J SHALL 仅为已enrolled专用v4 reserved身份提供管理员HTTP runtime元数据、rotate、节点级revoke及非秘密操作查询。rotate MUST 核对identity/latest-issued-record（包括expired/revoked）/维护CAS及disabled/无未结Attempt，严格0–600秒、最多新旧两份且不延旧截止；revoke可以在active/unknown时撤销节点全部HTTP行但不动B/AMQP/H或原Slot/Lease/journal/预算/ACK。维护版本、instance/generation、原proof期限和I文件version3恢复资格保持。

#### Scenario: 同nonce并发与提交未知
- **WHEN** 同nonce并发、已提交后重复POST或原事务确证回滚后显式重发
- **THEN** 同nonce至多一次持久签发，已提交重复只返非秘密回执、不重发原文；确证回滚无原nonce/凭据行，显式新请求须当前全部资格。未知客户端仅GET观察，absence不证明回滚、不自动POST/换nonce，原文丢失须另一次显式recover

### Requirement: J原文交付兼容严格安装格式
J首次成功201正文 SHALL 只有worker_id/worker_identity_id/credential_id/credential_record_id，Location所指GET提供有界非秘密元数据；原文只在commit、独立读回及当前权威核对后交付一次，原审计回执以0089保存唯一nonce/请求指纹/≤2KiB不可变非秘密结果。MUST 不保存可重发secret/密文/digest或在普通错误中暴露它；200重复回执及非201不得覆盖安装文件，不放宽原安装器schema。

#### Scenario: 私有安装与旧lease失效
- **WHEN** 受信操作者以原0600文件/0700目录和既有CLI安装，Agent在原心跳点重载
- **THEN** 保持同identity及单调记录、原instance/proof截止和文件锁/fsync/未知保留；不自动activate，不新增安装确认握手，零窗口/迟到安装不得复活已失效lease或unknown

### Requirement: J只新增I到J同事务发布
J SHALL 只接受经逐镜像可信映射的I来源c9b21a4ae3f05c4e382e070fa180dbd07f58d350、227cc637292f23cb8432cd10ba24f06a9090546e、4e513d1ceb34999c45c9ca42f08135704f72faa7，清洁排空/真实停机及原owner围栏下，以同连接事务执行0089 DDL/版本、authority CAS/revoke/append。MUST 保留原数据/身份/凭据/namespace及各祖先原schema/摘要，最多五笔封闭相邻链、四套材料、第六行拒绝；旧阶段新apply、任意跳升和降级不获得资格。

#### Scenario: DDL失败或COMMIT回复丢失
- **WHEN** 迁移/权威写入失败或提交不明
- **THEN** DDL/版本/权威/回执共同回滚或提交；关闭写连接后独立observe，裂分阻断、absence不作回滚，已提交仅前向安装J，不复用旧unknown或伪造来源取得资格

下方I及更早发布对属于对应历史二进制合同；当前J写资格仅为本节I→J。真实J4及发布结果另按适用候选记录，不由规格同步宣称完成。

### Requirement: I文件模式默认禁用且恢复资格独立
W1-I SHALL 在单Control、K=1/2、strict fixed/Slot 0、无依赖Python/JavaScript及manual/schedule范围增加托管文件；Worker可信input mode仅接受disabled/managed_files，默认disabled。MUST 经当前启动内受限文件写入/核对/清理探针后才报告可缺省严格bool managed_files_v1，缺失=false，新实例清除旧事实；能力不授予Artifact或H业务Credential权限。文件与H绑定可组合，但H冻结引用、节点许可及结果净化上限全部保持。

#### Scenario: 缺能力、非法文件或当前配置变化
- **WHEN** 目标未报告本次文件能力，或文件数/字节/元数据不合法
- **THEN** 在新Execution/Admission/Outbox/Lease前拒绝；已接受责任沿原快照/Lease及固定目标等待或失败，不替换当前配置、不增加文件级重试

### Requirement: I文件version3仅供同次活动责任结账
W1-I文件 SHALL 使用私有prepared/stopped/terminal version3，封闭记录最多4项受控mount名、size/hash和对象绑定，不含原文件名/正文/Secret/Token。MUST 只在原同进程owner、物理停止及当前授权成立时走正常监督/cleanup；B/C跨进程恢复入口拒绝version3，崩溃残留保留unknown，不重新下载或运行业务。none/JSON保留原prepared/stopped v1、正常terminal v1及C处置terminal v2。

#### Scenario: 文件准备失败与关闭未知
- **WHEN** 原准备期限内文件失败，或原期限已到
- **THEN** 只有同一nonbusiness staging child/creator/fd/IO实际结束、原guardian确认空cgroup及当前v4授权成立，才凭独立“业务未启动”观察走原净化/结果提交/独立观察/cleanup/本地结账及自身ACK；冻结Retry Policy/截止/退避和真实Attempt保持，未证明条件继续保留全部责任

### Requirement: I只增加精确H到I封闭发布对
W1-I SHALL 保持schema0088、原DB/卷/cluster/epoch/key/四指纹/K、Worker身份及凭据，来源仅为H main 9d7cd67f483950d4a6f96ebc9342005636f61348、最终PR HEAD ed925887f28704001117825d0d46488dacbfe595及原H真实制品72f2aa23bd9b680f820d16b82283b950533c6775，并逐个绑定已核实声明/构建/镜像digest。MUST 以至多3套原私有前序核验fresh-H/G→H/F→G→H/E→F→G→H的完整链，最多4笔、第五行拒绝；保留原摘要，核对全部祖先ABA/latest/authority和相邻声明，原锁序下CAS/revoke/append及提交未知只读观察、提交后前向安装。不新增E/F/G写资格或任意递归版本链。

#### Scenario: 最长合法来源或旧意图
- **WHEN** 完整原生E→F→G→H及原私有三套前序可核，旧业务/IO/授权已清洁停止
- **THEN** 只能以当前G→H operation追加H→I；历史回执/摘要保持并可只读观察，I二进制不重新apply旧阶段；缺前序、任何祖先ABA、分叉/超长/unknown均拒绝，启动新generation后仍须显式activate

下方H/F/G发布对要求记录对应历史二进制；I写资格以本节H→I为准，不继承旧阶段写许可。

### Requirement: H业务凭据能力与节点使用许可独立
W1-H SHALL 默认禁用业务凭据处理，可信Worker配置仅接受disabled/bindings；ready的可缺省business_credentials_v1为严格bool，缺失=false，沿原提交后完整独立观察保存于独立能力键，新实例清除旧观察。Control MUST 另核对以下私有部署许可：version=1、最多2个Worker、每节点最多16个Credential ID、文件最多16KiB，worker_id与稳定identity同时匹配；名称、语言、标签、自报能力、HTTP/AMQP身份凭据或Pool不能自动授予业务Credential权限。

#### Scenario: 旧报告与无凭据任务
- **WHEN** 合法当前实例未报告业务凭据能力或配置disabled
- **THEN** 不授予业务凭据路径；符合原G条件的无凭据Python/JS任务及其维护操作仍按原合同工作，不把缺省解释成许可

#### Scenario: 自授权、报告迟到或类型混淆
- **WHEN** Worker自报business_credentials_v1但无匹配许可，或以旧generation/旧证明、数字/字符串代替bool报告
- **THEN** 不获得新增授权或ready观察；不能继承上一个实例的业务凭据能力，相关不兼容新任务在承担责任前拒绝

#### Scenario: A冒用B或普通用户读取凭据
- **WHEN** A以自己的身份领取B的Execution/冻结凭据，或普通用户请求凭据真值/修改绑定
- **THEN** 沿原Worker/Attempt归属、证明和现有Credential/Adapter owner/admin ACL拒绝，不返回真值；合法绑定及代码执行者可使用其已获许可值，不宣称API元数据隔离能阻止授权代码读取

### Requirement: H部署许可固定且秘密只在必要执行边界可见
W1-H SHALL 从可选私有DLR_WORKER_BUSINESS_CREDENTIAL_POLICY_FILE读取封闭、0600且受信所有者的有界配置，防链接/替换并在单Control启动时冻结同一非秘密策略/digest供各入口及有界child使用；无配置为空许可，已配置而非法/不可读时拒绝受影响启动。MUST 不热重读更宽策略、不新增策略UI/服务，不复用四项namespace/capacity指纹表达许可。Master Key与Worker运行/proof/AMQP凭据不能注入业务；业务值不进入Broker、普通API、浏览器、AI Prompt或持久journal/结果历史。

#### Scenario: 策略丢失、被改写或试图热放宽
- **WHEN** 已配置策略不完整/超限/字段或身份重复，或运行中替换私有文件
- **THEN** 非法启动不默认全允许；运行请求和Claim child只使用原受信冻结版本，许可变更须停止新责任、排空及受控重启，不从请求体或Worker报告取得策略

#### Scenario: 业务值轮换或原实例失权
- **WHEN** 管理员更新Credential真值，或当前Worker/Attempt凭据及证明失效
- **THEN** 已提交任务不会自动换值，失权按原监管器停止与unknown合同处理；更新/删除不宣称即时收回已发出的业务凭据，必要的上游撤销与原取消是独立操作，H不新增此类轮换/强制清账服务

### Requirement: H只增加有界且精确的G到H发布对
W1-H SHALL 保持schema0088、原转换表和K=1/2，仅将经制品核实的G main `a4a9da204659c36ec6b843bb28c8f13e5f355582` 或已核实同产品输入 `530ab2e8e5e7efaa7a1492a2c754a461cb6d4ba7` / `8d216d3a052a6643fdf1bee74b1538e1ddd8a891` 来源升级至本批H。仅允许fresh-G→H一笔、F→G→H两笔、E→F→G→H三笔；MUST 以最多两套有界原私有source/intent重建原摘要并核对完整相邻声明及当前latest/authority，最多查询四行以拒绝更长历史，不新增迁移、任意递归升级、旧E/F新apply或反向回退。

#### Scenario: 合法已有F到G前序或最长来源
- **WHEN** 当前G分别具有唯一F→G root，或唯一E→F root和F→G child，且所有原私有意图、发布来源及摘要匹配
- **THEN** 只能以当前F→G operation为previous_id追加一个H，旧回执/摘要/数据保持；fresh-G空表只在实际G制品可核实时允许新root，空表和同schema本身不构成来源证明

#### Scenario: 多余历史、伪前序或旧版本跳升
- **WHEN** 存在第四笔、分叉/循环、缺失前序文件或摘要不符，或试图E→G/F→H/H→G及其他来源
- **THEN** 拒绝并保留原数据/权威；不只放宽LIMIT或把旧表不存在的SHA当作可读证据，不SQL补回执获得资格

### Requirement: H后继保留原子围栏和全部祖先ABA防护
W1-H SHALL 沿原清洁停机/角色/lease/物理前提和exclusive gate→authority→完整chain→旧Control实例锁序，在同事务CAS源及latest、永久revoke旧实例并append；read_intent、check、apply及observe均核对目标SHA不回到已知E/F/G发布，目标digest不回到任何已提供祖先的source/target。MUST 在writer关闭后独立观察原operation ID，仅committed_current可前向原子安装H与匹配私有策略，原Worker新generation仍disabled并显式activate。原身份、四指纹、Master Key/proof、Credential ciphertext/引用/历史及卷保持，不改K或抹去未决责任。

#### Scenario: 并发、ABA或提交响应丢失
- **WHEN** 两个G→H意图竞争同前序、目标回到任一祖先digest，或COMMIT结果未知
- **THEN** 最多一个合法后继提交，ABA在写前拒绝；未知先独立读回而非换ID重写，不能把superseded或旧E/F/G历史回执当作当前H安装许可

#### Scenario: 已提交但声明尚未安装
- **WHEN** H回执已持久提交而本机仍为G声明，或旧G实例尝试恢复
- **THEN** 保持停机并前向安装/启动匹配H，旧实例/旧声明不能续权；提交前且源权威未变才可终止操作恢复G，提交后禁止直接回退G

### Requirement: 第二语言必须由当前实例在原资源内预检后封闭报告
W1-G MUST 保留可信 Agent `language_mode=python` 默认值且不要求 Node，仅显式 `python_javascript` MAY 报告双语言；未知模式 MUST 拒绝启动。双语言 MUST 在可信固定 `/usr/local/bin/node`、Node 22、原标准任务硬预算与实际 Sandbox 内完成完整 ESM harness 小样本和物理清理后才发送 ready，失败 MUST NOT 降级虚报新 ready。ready MUST 仅接受规范 `['python']` 或 `['python','javascript']`，旧缺字段报告只为 Python；重复、乱序、未知字段/语言拒绝。新 instance MUST 清空旧能力，Control MUST 在原事务与独立提交观察中核对完整同一集合。

#### Scenario: Python-only 自定义 Worker 没有 Node
- **WHEN** 使用默认 Python 模式启动且原 Python/隔离预检通过
- **THEN** 保留原 ready 路径，不执行 Node 探针，不由 Control 或镜像名推断 JavaScript 能力

#### Scenario: 显式双语言预检失败或报告不合法
- **WHEN** Node 不可用/版本或可信路径不符、标准硬限内运行或清理失败，或报告未知/重复/乱序能力
- **THEN** 不获得新的双语言 ready；旧/过期实例证明不得恢复资格，心跳不增能力、不激活 disabled

### Requirement: JavaScript 必须复用原有界执行与责任链
W1-G MUST 仅增加固定 Worker 上无第三方依赖 JavaScript 的 manual/schedule、none/JSON 路径，沿既有 ESM `handle(context, input)` 和结果/日志合同。接收、schedule 启用/触发、派发及最终 Claim MUST 核对冻结任务与固定目标的封闭语言能力；JS 按 npm、Python 按 PyPI 检查 builtin 包源。requirements、dependency check、材料/文件、credential bindings、secrets 和其他来源/语言 MUST 保持拒绝。已接受 Execution MUST 保留原快照，能力变化或资源等待不得改写它、不建新 Attempt、不消耗执行重试、不 fallback。

#### Scenario: 执行截止在 Sandbox 准备阶段到达
- **WHEN** 原执行定时器明确到期，没有同时观察到父进程、控制通道或辅助进程异常，且原监管器回收、cgroup 归空和输出排空均已独立确认
- **THEN** 沿原超时结果、物理清理及 ACK 链完成，不延长预算；准备失败、时间比较或缺失可信停止观察 MUST 保留 unknown，不回填旧 stopped 或扩大 B/C 资格

#### Scenario: JS 任务指向 Python-only 节点
- **WHEN** 创建新执行/启用 schedule，或已接受工作派发/最终 Claim 时目标不具对应能力
- **THEN** 新责任前明确拒绝；旧责任有界等待并保持可读，容量忙与能力不匹配分开，固定路由不迁移

#### Scenario: JS 准备与运行
- **WHEN** 受支持任务经两阶段 Claim 领取
- **THEN** 业务 payload 保持在原有界 child，按指纹/目录 FD/预算生成 adapter.mjs 与原 Node SOURCE；sealed launch 仅固定 Node/harness/workspace/adapter argv 与最小环境，不经 shell、不继承 NODE_OPTIONS/NODE_PATH/npm 配置、不安装包/cache/node_modules；原监管器、硬资源、结果 proof、物理清理、结账和 ACK 顺序保持

### Requirement: 第二语言不扩大恢复和维护授权
W1-G MUST 保持 B/C 检查点 version、MAC、实际 inventory/binding 和原同宿主/boot/原卷/旧进程停止/平台终态资格；仅将文件白名单扩为匹配 dispatch 语言的 Python、JS 或共同文件实际子集。旧 Python 缺部分模板文件 MUST 仍可读；混合语言、未知文件、symlink、不一致 binding 或缺可信 stopped MUST 拒绝。激活及现有 UI MUST 使用同一封闭集合，同时保留维护版本、权限、隔离/无未结责任、新鲜度、未知回执和 K=2 观察上限。

#### Scenario: partial staging 或未知快照
- **WHEN** 只有部分 JS 文件而没有可信 stopped，或页面能力未知/过期
- **THEN** 不补造恢复资格、不开放管理；普通用户仍只读，显式 activate/drain 沿原 request ID / expected version 合同

### Requirement: F到G仅允许原回执表上的一个精确同schema发布对
G 二进制的新 apply MUST 仅接受 F main `0417b82c4ab6f76aefdf3eafe38327f46cb18358` 或原交付已核实同产品输入的 F 制品到本批匹配 G 制品，源目标 schema 均为 `0088_issue129_image_transition`，MUST NOT 新增迁移。来源 MUST 是合法 fresh-F 空回执表，或当前 authority 匹配唯一合法 E→F root 的 F 目标；后者 MUST 以 owner-only 有界原意图/原 E 声明重建摘要并逐字段核对原 root/current latest，仅追加一个 G child。原表没有发布 SHA 原文，MUST NOT 用任意标签或 SQL 伪造来源。

#### Scenario: 前序未知、陈旧、ABA或已有后继
- **WHEN** 原意图/制品不可核实、链形状不合法或超过两个回执、当前 authority/前序不匹配，或另一个 G 已提交
- **THEN** 不新增转换、不分叉、不反向 CAS，保持原数据和停机围栏；同 ID 内容完整匹配的已提交重入只读回原回执，不重复 revoke 新实例

#### Scenario: 同schema转换提交未知与历史回执
- **WHEN** F→G 语句/提交回复未知、安装中断，或读取原 E→F 回执
- **THEN** 保留原 F 清洁/物理停止/租约/角色前提，沿 schema gate→authority→前序/latest→旧实例锁序原子 CAS/revoke/append；关闭原写连接再独立 observe，只有 G committed_current 可安装启动；原 E→F 已被取代者为 committed_superseded，不授予回装 F 或新做 E→G 的许可

#### Scenario: 原F来源进入G并恢复业务
- **WHEN** 从合法来源在原库/原卷/原身份完成 G 转换、匹配制品安装和显式激活
- **THEN** 按冻结 G1–G5 证明两独立 Linux Worker 宿主上 JS manual/JSON 与默认 Python manual/none 并行、一次 JS schedule/none 及效果/Attempt/Slot/结果/cleanup/ACK；制品/原历史/首败分别记录，不用旧 F 通过或文档校验替代 G 验收

### Requirement: 停机换镜像必须绑定精确发布对并保留稳定身份
历史 W1-F 镜像 MUST 仅支持 W1-E main `a37c9d3e19985acddb1f3e33ff6d3c7337147270`、schema `0087_issue129_disposition` 到经核实 F 制品、schema `0088_issue129_image_transition`；G 的后继以本规格 W1-G 要求为准，不把旧首跳放宽为 E→G。可信操作者 MUST 冻结各组件 SHA/digest、协议/检查点兼容和真实制品来源，MUST NOT 用声明 digest 代替镜像或物理证据。原 cluster/epoch/proof key、三个私有凭据、四项指纹、K、PG/Broker/队列、Worker ID/凭据/固定绑定及原卷 MUST 保持。

#### Scenario: 未支持的源或目标改变不可变条件
- **WHEN** 源发布对不符、意图字段重复/超限、凭据/指纹/epoch/key/K 改变，或无法核实原身份与原卷
- **THEN** 保持停机并拒绝转换，不借普通 init、重 enrollment、换空卷或旧 digest 继续

### Requirement: 升级资格必须同时具备清洁责任和独立停写事实
操作者 MUST 先停用 schedule、持续封闭新增责任入口，完成既有任务与 drain/disabled，物理停止全部 Control/Worker/后代和 IO、排除自动重启及共享写入。CLI MUST 复核专用来源、无 queued/running/retry_wait、未结 cleanup/Attempt、占用 Slot、活动输入 Lease、待发布 Outbox、未处置 DLQ、非零 Admission 和所有旧 nominal/guard/proof 期限。runtime MUST NOLOGIN，唯一 owner 维护连接、无其他客户端/已准备事务；管理员 MUST 保持角色/授权与写入口围栏。

#### Scenario: 已 disabled 但仍排队、旧期限有效或存在残留会话
- **WHEN** 任一数据库拒绝条件成立，或 Broker/journal/Control retained/creator/IO/浏览器意图、物理停止和原卷独占事实未知
- **THEN** 拒绝升级，保留原责任；disabled、时间经过、NOLOGIN 和证据摘要各自不能代替其他条件，不 purge/取消/删行凑空

### Requirement: 转换必须由独立本地 owner 事务原子提交并可前向重入
默认 init MUST 仍只初始化/核对原 authority。本地 check/apply/observe MUST 只读取有限、封闭、owner 0600 私有源/目标声明和固定 operation/前序意图，不启动应用后台或增加公开 API。普通 runtime/Worker/Web admin MUST 无转换权限。首次迁移 MUST 只添加 append-only 回执表与目标 revision，不先改 authority；两个版本在 schema/authority 中间态 MUST 拒绝服务。apply MUST 在有界 exclusive schema gate → authority → receipt/旧实例锁序下以 READ COMMITTED 顶层事务复核全部前提，CAS digest/schema/前序、唯一回执及永久 revoke 旧 Control 实例原子提交，不修改业务结果、Worker 身份或维护版本。

#### Scenario: 同 ID 冲突、竞争或迟到 ABA 请求
- **WHEN** 相同 operation 内容不同、前序已变化，或另一个转换已经提交
- **THEN** 拒绝冲突，不分叉；匹配的已提交重入只读原回执，不再次 revoke 新实例，已结历史与独立 Hold 保持

#### Scenario: 迁移或转换回复丢失
- **WHEN** 迁移/语句/COMMIT 结果不明，或转换已提交而声明尚未安装
- **THEN** 关闭原写连接，以独立只读连接核对真实 revision/表结构及原 operation；精确源态才可重做指定迁移，精确目标中间态前向 apply；未知保持停机，不以预提交行、自读、RETURNING、CLI exit 0 或相同目标 digest 宣称提交

### Requirement: 独立回执观察必须约束声明安装与服务恢复
observe MUST 以同一语句快照核对完整 intent、回执、链末端和当前 authority，区分 not_installed/not_found/committed_current/committed_superseded/unknown。只有 committed_current MAY 授权原目标声明的操作者原子安装、fsync/rename/读回；安装和启动前 MUST 再观察。schema 前进或转换提交后 MUST 仅向前完成，不自动 downgrade/反向 CAS。有回执或目标 authority 引用时 MUST 拒绝降级丢弃元数据。

#### Scenario: 原回执已被后续转换取代或安装中断
- **WHEN** 回执为 committed_superseded、读取失败/冲突，或安装/启动未完成
- **THEN** 禁止用旧回执安装旧声明；保持原 ID、私有输入、数据与角色围栏，未知不猜测回滚；提交后安装前中断可原 ID 只读确认后继续

#### Scenario: 匹配目标恢复业务
- **WHEN** 当前回执确认、匹配 Control/Web/全部 Worker 已安装、owner 已退出且原启动源仍封闭
- **THEN** 恢复 runtime 登录、启动原身份/原卷的单 Control 和 Worker；Worker 仅产生新 instance/generation，保持 disabled 后逐个显式 activate；两节点各用新键验证唯一 Attempt/Slot、目标效果、cleanup 和 ACK，进程 online 不能代替业务可用

### Requirement: 专用部署必须先建立可信身份和有限资源
远程模式 MUST 默认关闭。启用时 Control MUST 拒绝旧 Worker 身份、非阶段来源的执行责任、不可确认的运行角色及第二个有效 Control 应用进程。部署 MUST 预建持久 journal 锚点，固定身份、信任声明、签名密钥及镜像，保留正常停启所用的原 runtime volume。Worker MUST 使用验证证书链和主机名的 HTTPS/AMQPS，不得依赖数据库权限、Control 业务共享目录或业务入站端口。

#### Scenario: 旧部署尝试直接打开开关
- **WHEN** 数据库存在旧身份或非阶段执行/Attempt 责任
- **THEN** 远程模式启动失败，原数据和默认 v3 操作路径保持可辨识，不自动迁移或清账

#### Scenario: 持久锚点缺失或资源条件不成立
- **WHEN** Worker 无法确认原目录、实际 cgroup 归属、有限硬资源或禁止 swap 条件
- **THEN** Worker 不进入可领取状态，不以新空目录替换原责任

### Requirement: 新任务与最终 Claim 都必须核对阶段能力
远程阶段 MUST 只接受固定到阶段身份的标准库 Python，以及 W1-G 显式双语言节点上的无第三方依赖 JavaScript 普通手动/定时任务及 none/JSON 输入。仅 W1-H 可按本规格的显式能力及冻结部署许可使用绑定业务凭据。第三方依赖、内置包、未许可业务凭据、托管文件、其他语言和其他触发方式 MUST 在新责任接受/启用之前拒绝。最终 Claim MUST 重新核对冻结快照；不兼容的旧责任不得被改写、删除或误报为已完成。

#### Scenario: 不支持的新请求
- **WHEN** 新执行或定时启用包含不支持的能力
- **THEN** 返回 `worker_task_capability_unavailable`，不新增对应 Execution、Admission 或 Outbox 责任

#### Scenario: 准备领取时预算不足
- **WHEN** 协商后的 wire/解析资源无法容纳冻结任务
- **THEN** 在提交 Attempt 前明确等待，不增加 attempt_count；已发生或未知的提交不得解释成这一等待

### Requirement: 每次有效操作都必须绑定当前身份和提交证明
protocol 4 操作 MUST 验证独立运行凭据、稳定身份、instance/generation、当前实例证明及适用的 Attempt/fence/Token。候选提交 MUST 经独立的及时观察才能形成授权证明；事务或回复未知、旧证明、期限已过时不得扩大权限。重复 Claim 不得产生第二个有效 Attempt。普通操作可共享 Token 命名空间围栏，凭据签发/撤销 MUST 保持独占。

v4 Worker MUST 在本地授权截止计算与签名观测校验中统一使用 5 秒安全余量。数据库实例租约为 30 秒，默认心跳间隔为 5 秒。一次完整租约从原请求发起起最多提供 25 秒本地授权，先前证明、签名剩余有效期或执行截止更短时取最早值。部署 MUST 核对当前主库及其可提升候选在租期和切换期间的时钟相对 Worker 单调经过时间的变化。混合版本期间必须同时满足仍在运行的旧 Worker 所需的时钟条件。

#### Scenario: 签名观测超出时钟余量
- **WHEN** 相邻签名观测或滚动租期内的保守偏差边界超过 5 秒，或证明在原本地截止后到达
- **THEN** Worker 停止旧业务并保留原责任，新回执不得恢复已失效会话

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

### Requirement: 受限管理员观察必须区分状态与执行权威
W1-D 管理员 status 和精确 drain/activate 回执 MUST 由独立只读会话的单条业务 SELECT 读取已提交快照，返回有限 schema、observed_at、身份/代际、调度/容量、活动和未结计数、历史阻塞及当前操作摘要。读取 MUST NOT 写入、取得业务行锁、推进版本或输出恢复 binding、宿主材料、MAC、路径、正文及凭据。普通 Worker 列表及权限保持；Worker 凭据不得读取管理员明细。

#### Scenario: 运行中或维护中的 v4 节点
- **WHEN** 现有系统状态页展示受限 v4 Worker
- **THEN** 不使用 v3 Broker 标志否定 v4，也不将有效运行责任误报为历史阻塞；协议、在线、维护、容量和责任分别展示，正常观察不授予 Claim 权限；v3 就绪和缓存入口保持

#### Scenario: 观察不可用或过期
- **WHEN** 隐藏恢复、30 秒未成功更新、缺字段/旧 Control、403/404/503 或读取失败
- **THEN** 旧观察仅作历史值，新增写按钮不可据此放行；共享单飞刷新不按行增加轮询，隐藏页面不新增维护读取，旧响应不得覆盖新身份/代际/版本

### Requirement: 标准维护界面必须显式保存意图并独立读回
排空/确认/激活 MUST 在每次确认前重新读取观察，绑定非零 request ID、expected_version、稳定身份及当前操作者。POST 前 MUST 在该页 sessionStorage 暂存并回读非敏感 tuple；失败不发 POST。提交未知后 MUST 先精确 GET，刷新仅恢复 tuple 并查询，不能自动换 ID/版本或重发。历史 operation 与 current 状态 MUST 分开展示；404 不证明回滚，浏览器观察不替代服务器锁内鉴权与资格裁决。

#### Scenario: 排空登记后责任归零
- **WHEN** 原操作者明确确认本次 drain，原 tuple 匹配且当前版本仍等于 operation.result_version
- **THEN** 仅在未结归零后转 disabled，重复确认不增长版本；GET 或轮询不完成排空，旧周期 drain 一律 409 且零写入

#### Scenario: B/C 恢复留下 draining
- **WHEN** 当前维护操作为 reconcile，容量 ready、已 enrollment、零未结且无历史阻塞，管理员使用当前版本和新 request ID 明确确认
- **THEN** 版本增长一次并转 disabled；未结或 reconciling 拒绝，不修改原恢复、处置、清理或 completion

#### Scenario: activate 提交后页面丢失响应
- **WHEN** POST 超时/断线/503 或页面重入
- **THEN** 以原 tuple 只读查询；用户明确重试时只发原 tuple，已提交且被后续版本替代时不宣称当前 active；登出/切换 Principal 清除意图，其他操作者不能冒用原 drain

#### Scenario: 后续激活仍存在 C 处置
- **WHEN** status 返回最近 C 摘要和原 Execution/Attempt 终态
- **THEN** 界面持续显示原业务结果未知、结清不重试也不回滚；disabled/零计数/在线均不能替代物理停止、本地预算或当前 channel ACK，界面不提供强制清账或恢复向导

### Requirement: 双固定 Worker 必须使用有限且精确归属的传输槽
W1-E MUST 仅支持部署级 K=1/2、默认 1，在同一 Control 内预分配 K 个槽，每 Worker 最多一个。spawn 前 MUST 计费，Claim→结果 stage/finalize MUST 原位保留同 child 和预算；关闭、回收未知及 creator 未结束仍占名额。全局登记锁 MUST NOT 跨 IO、进程等待或业务事务。Control MUST 验证并固定实际 cgroup/namespace、内存和 PID 限额，满足 `memory.max >= K × child地址空间 + 128 MiB`、`pids.max >= K+1`、无 swap，并保持现有有限 CPU/PID/连接总边界。

#### Scenario: A 保留结果槽时 B 准备任务
- **WHEN** K=2，A 的 Claim 已提交且保留结果 child，B 具有独立固定路由、凭据和当前证明
- **THEN** B 可以取得另一槽并提交自己的唯一 Attempt；proposal、身份/代际、Attempt/fence、nonce、超时及清理不跨 Worker 复用；各自 result 使用原槽，无 entry 的合法重入也先取 K 内名额且不重做 Claim

#### Scenario: 新 prepare 确定在 Claim 事务前满槽
- **WHEN** 当前 Control 权威和总资源已核验，但 spawn 和 Claim 事务前没有名额
- **THEN** 仅本次返回 HTTP 200 六字段 DEFER：`control_transport_busy`、retry=1、attempt/payload=null、cancel=false，无 proposal/proof、Attempt、Retry 消耗或 ACK；原 consumer 有界退避，该响应不证明旧未知请求回滚

#### Scenario: 子进程关闭或权威失效
- **WHEN** A 的 IO 失败且可确认回收，或任意槽 child/creator 无法确认回收，或共享 Control 权威/总资源失效
- **THEN** 可确认的 A 关闭只释放 A；未知回收保留计费并全局拒绝新 Claim/Result，权威/资源失效和已发 finalize/结果提交未知仍走原 503/未知，不伪装 DEFER；旧 timer、CAS、重复 finally 不关闭新槽或 B

### Requirement: 双固定 Worker 的维护和浏览器意图必须独立
A/B MUST 使用不同固定 Adapter、独立 HTTP/Broker 凭据和队列，单 Worker 仍为 Slot 0、prefetch=1；A 排空只拒绝 A 新 Claim，A 旧责任不自动转给 B。共享目录中管理员 MUST 最多观察两个 v4 节点，逐个 GET、全局最多一个在途，各有 5 秒超时、30 秒新鲜度及身份/代际/版本防乱序；单节点失败不污染另一节点。超两节点或目录不可信 MUST 禁止新维护写入，普通用户不读取管理员明细。

#### Scenario: A 未知时操作 B 或从旧存储升级
- **WHEN** 同一 tab 当前认证范围存在 A 未决 tuple，用户明确操作 B，或恢复旧 v1 tuple
- **THEN** 固定 v2 容器最多保存两份非敏感 tuple、总计 8192 字节，每 Worker 一份且按稳定身份区分，不覆盖或驱逐未知；旧 v1 严格校验并持久化 v2、完整回读相等后才删除；冲突或存储失败保留原记录并零新 POST，刷新只读，同 tuple 显式重试，登出/Principal 改变清除两版本，迟到响应不清除另一份记录

#### Scenario: 启用两槽或回退为单节点
- **WHEN** 部署匹配 Control/Web，显式改变 K 或回退旧 Web/单节点
- **THEN** 先排空并核对所有业务、结果、原卷记录和真实 child/creator 已结束后停 Control，K 只能离线改为 1/2；回退旧 Web 前 v2 无未决 tuple，保留第二节点的绑定、历史和排队责任，不靠重启、删记录、降 K 或转给 A 假装结清；B/C、默认 v3、协议和数据库合同保持，不声明 Pool、预留或 HA

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
Worker MUST 独立核验其可观察的原锁/锚点、相同 boot/存储/身份、检查点和实际对象/挂载；新 namespace 观察不得覆盖旧绑定。宿主重启、复制卷、跨宿主接管仍延期；未提交结果仅允许下述 W1-C 完整停止检查点与既有平台终态子集，其他未知责任仍延期。

#### Scenario: 显式恢复具有完整资格的已提交结果
- **WHEN** 当前有效凭据读回匹配管理员意图、旧实例及候选期限已过，并且原卷检查通过
- **THEN** 只注册新的 reconciling 实例，不开 consumer、不报告 ready；用当前证明、原 Cleanup Token、两代历史和新 nonce 只读核对已提交 result intent/MAC/finalization，不使用旧证明或重发结果正文

#### Scenario: 清理或本地多文件删除中断
- **WHEN** 原对象的清理、cleanup 提交/回执或 journal/runtime 删除中断
- **THEN** 每项破坏操作前持久保存绑定原对象的删除意图，缺失对象只沿匹配意图重入；同名替换、嵌套挂载、损坏/额外对象仍阻断；已提交 completion 只读确认，未提交意图才可在当前围栏下重绑定；终结记录保存原 Cleanup Token、Dispatch/两份冻结额度、稳定结果/结算 ID 及精确记录摘要，逐项 unlink/fsync 后最后删除

#### Scenario: 恢复完成后重新调度与旧消息确认
- **WHEN** 物理清理、服务端结算、本地记录及实际管理 IO 已全部结束
- **THEN** 不对旧内存句柄 release 或将旧额度加回新账；重新验证 envelope/preflight 后建立一次新预算，保持维护状态并等待显式 activate；新 channel 对重投消息取得权威 ACK_NOOP 后才 ACK 当前 tag，再执行新任务，原 Execution/Attempt/业务结果不重建或修改


### Requirement: 未提交结果只能在明确平台终态下显式处置
W1-C MUST 仅接受同宿主同 boot 原身份原卷、完整真实 prepared/stopped、匹配 staged result intent/MAC 且没有任何 result finalization 的责任；允许组合只有 Attempt=worker_lost 与 Execution=dead_letter/cancelled，或 Attempt=cancelled 与 Execution=cancelled。running/retry_wait MUST 先沿原 cancel/reconciler 收敛；新 Attempt/dispatch、缺失/冲突/部分 finalization 和无 stopped 继续拒绝。已提交结果只按 B 资格处理，不能以 C 绕过取消冲突。

#### Scenario: 原策略允许 worker_lost 重试
- **WHEN** 原 Attempt 失联后进入 retry_wait，管理员选择结清未知结果
- **THEN** 原 cleanup 未结屏障继续阻止重试派发；先显式取消并读到允许终态，再登记 C，不增加原 attempt_count/dispatch 或隐式 Replay

#### Scenario: 外部目标可能已产生效果
- **WHEN** 管理员请求 C 处置
- **THEN** 必须确认平台未确认业务结果、结清不重试也不回滚；原 status/output/error/ended_at、Attempt 状态/计数及独立 Hold 保持，未知结果原因可查询，清理不显示为业务成功

### Requirement: 处置登记与提交观察必须独立冻结精确责任
C MUST 显式使用 settle_stopped_uncommitted，绑定原 B binding、stopped 摘要、staged intent/MAC、预期终态、固定未知结果确认、request ID 和 expected version。登记 SHALL 按 Admission→Execution→Attempts→Slot→Worker→maintenance operation→result intent 锁序重读，原子保存唯一非零 disposition ID、当前完整事务号及封闭不可变快照；数据库约束/触发器和运行角色围栏防止绕过。默认 committed_result 保持 B 合同。

#### Scenario: 管理员登记回执丢失
- **WHEN** 操作者以相同 request ID、身份和完整参数重试
- **THEN** 只读回同一 disposition/maintenance version，冲突拒绝；管理员 HTTP 成功不能替代 Worker 的独立提交观察

#### Scenario: 本事务可见或旧权限观察
- **WHEN** 处置仅在当前事务可见，或 xid/xmin/epoch、当前凭据/实例证明、原 Cleanup Token、两代 history、nonce/期限不匹配
- **THEN** 不签发历史权限、不删除物理对象或释放责任；COMMIT 或回执未知不推断为回滚

### Requirement: 证明与本地终结记录必须区分处置和结果
历史观察 v2 MUST 签名绑定版本、domain 和 kind，committed_result 携真实 finalization ID，platform_terminal 携独立 disposition ID；互斥且不能填假结果 ID。C MUST 绑定集群/epoch、原/当前身份、Execution/Attempt/fence、operation、prepared/stopped 摘要、原 staged intent/MAC、冻结终态、候选/期限、nonce 和可空 cleanup completion。B v1 证明和 terminal v1 仍只表示已提交结果。

#### Scenario: 交叉使用结果和处置
- **WHEN** kind/版本缺失或冲突、跨责任、旧 nonce/期限，或将 disposition 当作 finalization
- **THEN** 明确拒绝，不扩大历史业务权限或伪造业务结果

#### Scenario: C 清理回执或 terminal 部分删除未知
- **WHEN** 当前 generation 退出后新实例显式重入
- **THEN** 每次 stage/finalize/observe 重验已提交处置和当前权限，读回同一 completion；terminal v2 保留 kind/disposition/completion、原检查点/两份预算和精确清单，沿原 unlink/fsync 算法继续，最后删除锚点，不重复结账或重跑业务

### Requirement: C 升级与完成路径保留 B 的物理和预算边界
升级 MUST drain、停写并保留数据库/原卷，部署匹配 Control/Worker 与前向迁移；完整 B prepared/stopped 可以核验使用，无完整 stopped 不回填。存在 C 处置/terminal v2 时 MUST 拒绝降级、旧备份覆盖和只理解 B 的二进制。恢复不新增结果正文 spool、runner 或外围平台；物理清理和实际 IO、本地清单全部结束后才建立一次新预算并等待显式 activate。

#### Scenario: C 责任完整结清后继续工作
- **WHEN** 物理对象、服务端 Slot/Lease、completion、本地记录及两份冻结占用已各自确认结束
- **THEN** 新 channel 对旧消息仅按权威 ACK_NOOP 确认当前 tag，原业务键无恢复重执，后继新任务实际完成；安全阻断、同 VM 容器或 synthetic 检查点不能替代两个独立宿主的成功闭环
