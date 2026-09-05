## Purpose

冻结首期 17 个官方场景及其 51 个三语言 Recipe 的行为、安全、来源和真实性合同，使模板可以作为可读、可复制但不冒充已实连能力的可靠业务起点。

## ADDED Requirements

### Requirement: 每个 Variant 具有完整且等价的 Recipe 合同
每个 Scenario SHALL 提供 Python、JavaScript、Java 三个独立 Variant。三种实现 MUST 遵守同一输入、输出、分页、上限、错误和安全合同，但 MAY 使用各语言惯用 SDK 与结构。每个 Variant MUST 具有非空代码、建议依赖、安装说明、非敏感输入骨架、输入合同、输出合同、安全 Runtime 建议、成熟度和来源；建议项不得表示平台已安装 Dependency、已绑定 Credential 或已分配 Worker。

#### Scenario: 三语言静态完整
- **WHEN** 发布校验遍历任一 Scenario
- **THEN** Python、JavaScript、Java Variant 均具有全部必填 Recipe 字段和非空代码
- **AND** 三者声明相同的行为合同版本

### Requirement: 所有 Recipe 使用有界输出和非敏感配置
所有 Recipe MUST 对页数、记录数、字节数、行列数、文件数、批次数或执行时限中的适用维度设置正整数上限，并在超限前停止，返回完整 JSON 和 `partial=true` 或稳定错误，不得截断成损坏 JSON。真实 Credential MUST 只通过 DLR Credential Binding 注入；代码、runtime config、示例、Output 和普通日志 MUST NOT 包含或回显 Secret、Token、真实账号、真实 Endpoint 或本机路径。

#### Scenario: 达到输出上限
- **WHEN** 数据源继续返回数据但 Recipe 达到已配置的记录或字节上限
- **THEN** Recipe 停止继续收集并返回语法完整的有界结果
- **AND** 结果明确标记 partial、已处理数量和可安全提供的 checkpoint

#### Scenario: 外部调用失败
- **WHEN** 外部服务返回含 URL、Header 或认证信息的错误
- **THEN** Recipe 返回脱敏的错误类别与摘要
- **AND** 日志和 Output 不回显 Credential 或认证参数

### Requirement: 云与 CMDB 统一资产关系结构
7 个云资源 / CMDB Scenario 的 `preview` SHALL 输出版本化对象，至少包含 `assets`、`relationships`、`summary`、`partial` 和可选 `checkpoint`。资产 MUST 具有稳定 `external_key=provider:account:region:type:id`、`class`、`provider_type`、`name`、`account`、`region`、`zone`、`status`、`tags` 和 `attributes`；关系 MUST 具有 `from`、`type` 和 `to`，关系端点均引用资产 external key。关系类型首期 MUST 限于 `located_in`、`attached_to`、`protected_by`、`member_of`、`serves`、`routes_to`。缺失字段使用空值或空对象，不得编造未观测数据。

#### Scenario: 资源与关系稳定映射
- **WHEN** 两次采集返回同一厂商资源 id、账号、区域和类型
- **THEN** 两次输出生成相同 external_key
- **AND** 关系仅引用本批已知或以稳定 external_key 表达的端点

### Requirement: 云与 CMDB preview 有界且只读
7 个云资源 / CMDB Scenario MUST 原生支持 `mode=preview`。preview SHALL 只执行读取，受 `max_pages`、`max_records`、`max_bytes` 和请求总时限限制，MUST 返回 `partial`、摘要及可安全继续时的 checkpoint，不得调用目标 CMDB 写接口。云端示例权限 MUST 是最小只读权限，不得建议 Root 或主账号高权限。

#### Scenario: preview 不写目标 CMDB
- **WHEN** 用户以 preview 模式执行云或 ServiceNow Recipe
- **THEN** Recipe 只读取来源并返回有界资产/关系结果
- **AND** 不调用 begin、upsert、finish 或失效清扫接口

### Requirement: 云与 CMDB sync 使用稳定扫描输入和窄 Upsert 合同
7 个云资源 / CMDB Scenario MUST 原生支持 `mode=sync`，并依次执行 `begin_scan(scan_id)`、分页采集、分批 `upsert_assets`、分批 `upsert_relationships`、`finish_scan(scan_id)`。由于当前三语言 Runtime Context 不承诺跨 Attempt 的逻辑 Execution id，sync 输入 MUST 要求调用者提供稳定、非敏感的 `scan_id` 和 `source_scope`；同一 DLR Execution 的 immutable input 在整次重试中 MUST 复用二者，Recipe MUST NOT 在每个 Attempt 随机重生 scan_id。缺少或无效的任一值时 MUST 在任何目标写入前失败。

目标 Upsert 合同 MUST 以 `(source_scope, scan_id, external_key)` 和稳定关系键幂等，并对 begin、每个批次和 finish 接受稳定幂等键。只有所有来源范围和批次成功后才可调用 finish 并允许将本次未出现的旧资产标为失效；任一范围或批次失败 MUST 跳过 finish/失效清扫，返回 `partial=true`、失败范围、成功计数和 checkpoint。最终 Adapter Output MUST 只包含扫描、资产、关系、分页、失败和 checkpoint 摘要，不得回传全部资产。

#### Scenario: Worker 丢失后整次重试
- **WHEN** 同一 Execution 因 Worker 丢失而以相同 immutable input 重试
- **THEN** Recipe 复用相同 scan_id、source_scope 和批次幂等键
- **AND** 目标不会产生第二套资产、关系或扫描

#### Scenario: 批次部分失败
- **WHEN** 一个区域或 upsert 批次失败
- **THEN** Recipe 不调用 finish_scan，也不触发旧资产失效
- **AND** Output 仅返回有界失败清单、成功计数和 checkpoint

#### Scenario: scan_id 缺失
- **WHEN** sync 输入没有稳定 scan_id
- **THEN** Recipe 在 begin_scan 前返回可识别验证错误
- **AND** 目标 CMDB 零写入

### Requirement: 阿里云三场景按来源矩阵采集只读资源
阿里云计算与容器 Scenario SHALL 覆盖 ECS、云盘、网卡、镜像、伸缩组与 ACK；网络与流量入口 Scenario SHALL 覆盖 VPC、VSwitch、EIP、NAT、路由、ACL、VPN、SLB、DNS 与证书；数据库与中间件 Scenario SHALL 覆盖 RDS、Redis、MongoDB、Elasticsearch、OSS、NAS、RAM、KMS、ActionTrail、SLS 与安全中心中来源矩阵确认可由公开只读 API 稳定获得的资源和关系。每个资源族 MUST 在矩阵中记录固定来源、API、分页、补充查询、关系、缺口和许可证处理；未由来源或官方 SDK 确认的字段/关系不得输出。

#### Scenario: 阿里云覆盖矩阵与代码一致
- **WHEN** 发布校验对照三场景矩阵和 Variant
- **THEN** 每个声明支持的资源族都有可追溯调用、分页和映射入口
- **AND** 代码未声明矩阵中标为缺口的资源已支持

### Requirement: 腾讯云三场景按来源矩阵采集只读资源
腾讯云计算与容器 Scenario SHALL 覆盖 CVM、CBS、镜像、专用宿主机、伸缩组与 TKE；网络与流量入口 Scenario SHALL 覆盖 VPC、Subnet、ENI、EIP、NAT、路由、ACL、CCN、VPN、CLB、监听器与目标组；数据库与中间件 Scenario SHALL 覆盖 CDB、PostgreSQL、Redis、MongoDB、SQL Server、MariaDB、TDSQL-C、Elasticsearch、CKafka、COS、CFS、CAM、CLS、WAF 与证书中来源矩阵确认可由公开只读 API 稳定获得的资源和关系。每个资源族 MUST 具有与阿里云相同粒度的可追溯矩阵和真实缺口标识。

#### Scenario: 腾讯云缺口由官方 SDK 补齐
- **WHEN** 开源来源缺少某个已列资源族或实现明显过时
- **THEN** 该资源族只有在三语言对应官方 SDK/API 已核对后才可标为支持
- **AND** 来源记录区分开源行为研究与官方补齐

### Requirement: ServiceNow CMDB CI 快照遵守 Table API 边界
ServiceNow Scenario SHALL 默认读取 `cmdb_ci`，支持 encoded query、显式字段选择、display value 选择、分页和最大记录数。首期 MUST NOT 暗中读取 `cmdb_rel_ci` 或把关系拓扑标为支持。认证和实例 URL MUST 来自绑定/输入且不得回显。

#### Scenario: ServiceNow 默认快照
- **WHEN** 用户未指定表且执行 preview
- **THEN** Recipe 分页读取 `cmdb_ci` 并遵守字段与记录上限
- **AND** 不请求 `cmdb_rel_ci`

### Requirement: 通用 REST 单次请求约束副作用与脱敏
REST 单次请求 Scenario SHALL 支持 GET、POST、PUT、PATCH、DELETE，Query、Header、Basic/Bearer/API Key、JSON/文本请求体、超时、允许状态码和 JSON/文本响应解析；默认方法 MUST 为 GET。POST/PUT/PATCH/DELETE MUST 在文档和 UI 显示副作用警告，非幂等请求默认 MUST NOT 自动重试。重定向 MUST 默认保持同协议同主机；URL、Header 和 Query 中的认证值 MUST 脱敏。

#### Scenario: 非幂等 POST 遇到瞬时错误
- **WHEN** 用户未显式提供安全幂等策略且 POST 返回瞬时错误
- **THEN** Recipe 不自动重放请求
- **AND** 返回含副作用不确定性的脱敏错误

### Requirement: REST 分页具有四种策略与死循环保护
REST 分页 Scenario SHALL 支持 page、offset、cursor、next-url，支持 `max_pages`、`max_records`、请求总时限以及对 429/5xx 的有界退避和抖动。Recipe MUST 检测重复 cursor、重复 next URL、无记录推进与不增长 offset。next-url 默认 MUST 保持同协议同主机，跨源继续必须由用户显式选择且仍不得被描述为平台级 SSRF 防线。

#### Scenario: 服务端重复 cursor
- **WHEN** 两个连续响应返回相同非空 cursor
- **THEN** Recipe 以可识别循环错误停止
- **AND** 不继续无限请求或返回损坏结果

### Requirement: Webhook JSON 校验与标准化保持纯转换
Webhook Scenario SHALL 使用 Adapter 类型 `webhook`，支持必填字段检查、字段重命名、统一的点分隔嵌套路径、ISO-8601 UTC 时间标准化和统一输出对象。首期 MUST NOT 引入表达式 DSL 或 sync 模式；无法解析的路径或时间 MUST 产生字段级验证错误。

#### Scenario: 标准化有效载荷
- **WHEN** Webhook JSON 满足必填字段和映射配置
- **THEN** Recipe 输出重命名后的字段与规范化 UTC 时间
- **AND** 不发起外部同步或写操作

### Requirement: CSV 解析不绑定文件且具有资源上限
CSV → JSON Scenario SHALL 从执行输入提供的内容或文件引用读取，不在模板中绑定 Managed File。Recipe MUST 支持编码/BOM、表头、分隔符、空行策略、最大行数、最大列数、单字段字节数和总输出字节数；超限 MUST 明确失败或返回完整 partial 结果。

#### Scenario: Managed Input Store 关闭时使用 CSV Recipe
- **WHEN** 部署未启用 Managed Input Store 且输入直接提供受限 CSV 内容
- **THEN** Recipe 可完成解析并返回有界 JSON
- **AND** 不访问或创建托管文件实体

### Requirement: Excel 解析不得执行活动内容
Excel → JSON Scenario SHALL 支持 `.xlsx` 与 `.xls`、工作表选择、表头行、范围、空值处理、可用工作表列表、最大文件字节、最大行列数和总输出限制。Recipe MUST 以不计算公式值的模式读取，MUST NOT 执行宏、公式或外部链接；遇到加密、宏驱动或超限工作簿 MUST 拒绝或按文档标记不可处理。

#### Scenario: 工作簿含公式或外部关系
- **WHEN** 输入工作簿包含公式、宏或外部链接
- **THEN** Recipe 不执行或跟随这些活动内容
- **AND** 输出明确标记被拒绝或仅返回允许的静态值策略

### Requirement: JSON 映射清洗使用统一有限语义
JSON 字段映射与清洗 Scenario SHALL 使用 RFC 6901 JSON Pointer 作为三语言一致的嵌套路径标准，包括 `~0` 和 `~1` 转义与数组索引。Recipe SHALL 支持字段选择、重命名、默认值、`string|integer|number|boolean|datetime` 有限转换、等值/存在性过滤、按字段升降序排序和按字段保留首项去重。首期 MUST NOT 引入 JSONata、JQ、任意表达式求值或自建完整 DSL。

#### Scenario: 三语言处理同一 fixture
- **WHEN** 三个 Variant 接收相同数据和映射配置 fixture
- **THEN** 输出字段、顺序、类型转换、过滤和去重结果完全一致

### Requirement: PostgreSQL 与 MySQL 只提供受限只读快照
PostgreSQL 和 MySQL Scenario SHALL 各自支持一条参数化 SELECT、查询超时、最大行数、分批读取、只读事务和可靠关闭连接。Recipe MUST 拒绝多语句以及 INSERT、UPDATE、DELETE、DDL 和存储过程调用，并 MUST 文档要求数据库侧只读账号。SQL 值 MUST 使用驱动参数绑定；标识符不得由未验证输入直接拼接。

#### Scenario: 拒绝写 SQL 与多语句
- **WHEN** 输入 SQL 不是单条 SELECT 或含第二条语句
- **THEN** Recipe 在连接执行前拒绝请求
- **AND** 不尝试把写语句自动改写为只读

#### Scenario: 查询中途失败仍关闭连接
- **WHEN** 分批读取期间发生超时或驱动错误
- **THEN** 事务回滚且连接、游标可靠关闭
- **AND** 返回脱敏错误和已读取数量，不回显密码或完整 DSN

### Requirement: S3 兼容清单与读取均受界限保护
S3 Scenario SHALL 支持 endpoint、region、bucket、prefix、Continuation Token 和最大对象数，清单输出 SHALL 包含 key、size、etag、lastModified。对象读取 MUST 同时限制单对象字节数、对象数量和总输出字节数；没有受控 Output Artifact 合同时不得宣称支持任意大对象落地。Credential 必须来自绑定，endpoint 错误不得回显认证 Query。

#### Scenario: 对象超过读取上限
- **WHEN** 选中对象大小超过单对象上限
- **THEN** Recipe 不下载完整对象
- **AND** 返回有界元数据与清晰的超限状态

### Requirement: SFTP 验证主机身份并限制远端根目录
SFTP Scenario SHALL 支持 host、port、base directory、目录/文件过滤、最大文件数、单文件字节和总下载字节。连接 MUST 校验 known_hosts 或固定主机指纹，不得默认接受未知主机；所有规范化远端路径 MUST 保持在配置的 base directory 内，符号链接或 `..` 不得逃逸。Credential 必须来自绑定。

#### Scenario: 未知主机密钥
- **WHEN** 服务端主机密钥不在 known_hosts 且不匹配固定指纹
- **THEN** Recipe 在认证或读取文件前终止

#### Scenario: 路径试图逃逸根目录
- **WHEN** 列表项、符号链接或用户路径规范化后位于 base directory 外
- **THEN** Recipe 拒绝该路径且不读取内容

### Requirement: 来源矩阵与许可证处理可重复审计
目录 SHALL 提交资源/行为覆盖矩阵，至少包含来源 URL、固定 commit/tag、参考文件或 API、分页、关系、许可证、使用方式和核对日期。实现 SHALL 优先比较仍维护的独立来源；Apache-2.0 等兼容来源若直接改编 MUST 保留归属与所需 NOTICE，GPL、ELv2、无许可证或不兼容来源仅可研究行为。模板代码不得机械逐行翻译这些不兼容来源。

#### Scenario: 审核一个资源族的来源
- **WHEN** 审核者选择矩阵中的资源族
- **THEN** 可从 Scenario/Variant 追溯到固定版本、文件/API、分页、关系和许可证决策
- **AND** 目录中声明的支持范围不超过矩阵证据

### Requirement: 成熟度只由实际证据提升
`reference-generated` SHALL 表示仅依据来源重写且未执行；`syntax-verified` SHALL 要求该语言依赖解析以及语法/编译通过；`fixture-verified` SHALL 要求固定响应或本地 fake service 通过该语言的主要路径；`live-verified` SHALL 要求在真实外部服务以受控只读权限运行。发布至少 MUST 验证清单、Schema、代码非空、依赖/安装说明和所有声称的成熟度证据；缺少证据 MUST 降级标签而不是跳过失败。

#### Scenario: fixture 失败阻止成熟度升级
- **WHEN** Variant 标为 fixture-verified 但对应 fixture 测试失败或缺失
- **THEN** 发布校验失败，或在修正资产后将其降为真实级别
- **AND** 不得用另一语言或静态检查替代该证据
