# Template Recipe 使用与安全边界

## 目录身份

首期目录由仓库静态资产构成，随 DLR Control wheel 和容器版本发布：

- 5 个 Theme；
- 17 个 Scenario；
- 每个 Scenario 固定 Python、JavaScript、Java 三个 Variant，共 51 个；
- 列表和详情只读取元数据；只有用户选择某语言时才读取对应源码；
- 目录不会在启动后远程下载或静默更新。

仅浏览静态 Template 时，它只是可读、可复制的 Recipe，不是隐藏的系统 Adapter，也不会创建任何 Adapter、Slot、Execution、Outbox、Attempt 或 Worker 运行态对象。用户确认实例化后，事务只创建 Adapter 模型本身要求的 Slot 0 以及下节列出的最小对象；这不表示 Template 自身拥有 Slot。

## 复制后发生什么

用户在详情页选择语言、填写名称并确认后，Control 在一个事务中创建：

1. 一个归当前账户用户所有的 Adapter；无账户记录的部署 superadmin 沿用 system-owned 语义；
2. 必需的 Slot 0；
3. Task 的空安全 InputConfig，或 Webhook 的全新 disabled 配置和全新 public id；
4. 与所见版本和语言字节一致的 Revision 1；
5. 只读来源字段 `template_scenario_slug` 与 `template_version`。

成功后 Web 刷新 Adapter 列表、加载 Revision 1，并直接进入新 Adapter 的编辑页。

复制不会创建或继承：

- Credential 或 Credential Binding；
- 已安装 Dependency；
- Worker 分配；
- Schedule 或启用中的 Webhook；
- Managed File、Artifact 或 Lease；
- ACL 共享关系；
- Execution、Admission、Outbox、Attempt 或历史。

新 Adapter 默认停止、`run_mode=manual` 且没有 Worker。模板以后升级不会回写用户 Adapter。

## 运行前检查

复制只是形成可编辑起点。首次运行前需要人工完成：

- 阅读当前语言的代码、依赖、安装说明和成熟度；
- 选择语言兼容且满足部署隔离要求的 Worker；
- 通过平台依赖流程安装精确版本，不把“建议依赖”理解为已经安装；
- 通过 Credential Binding 绑定代码声明的 Secret 键；
- 用非生产、小范围输入先运行 preview 或纯转换；
- 核对输出上限、权限、目标副作用和重试行为；
- 云或 CMDB sync 额外确认目标实现 `dlr-cmdb-upsert/v1`。

## Credential 与敏感数据

真实 Secret 只能来自 `context.secrets.get(key)`。源码、输入骨架、runtime config、示例、普通日志和 Output 不得放入或回显密码、Token、私钥、真实账号、真实 Endpoint、本机路径或认证 Query。

目录中出现的域名使用保留的 `.example` 占位域；`EXAMPLE_...` 和 `REMOTE_ROOT` 是需要用户替换的明显占位符。

第三方错误必须收敛为稳定类别、有限摘要和安全计数，不直接透传原始异常、完整 URL、Headers 或响应体。

## 通用 HTTP 边界

- REST 单次请求默认 GET；
- POST、PUT、PATCH、DELETE 可能产生副作用，默认不自动重试；
- 只有目标提供业务幂等语义且用户明确配置时，才可安全重放写请求；
- Redirect 和分页 next URL 默认必须保持同协议同主机；
- Header API Key 使用 `DLR-Auth: api-key/<header>:<secret-binding>`；Query API Key 使用 `query_auth.parameter` + `query_auth.secret_binding`，两者都只在请求时从 Credential Binding 解析；
- 不得把 API Key 值直接写入 URL、普通 `query`、Header、日志或 Output。同名 Query 冲突必须拒绝，同源 Redirect 才可重新注入；跨源 next URL 会移除由 Binding 注入的 Query/Header 凭据；
- 直接 URL、普通 `query`、分页参数名和普通 Header 对凭据类名称 fail closed。名称小写并移除非字母数字后，只要包含 `accesskey`、`apikey`、`authorization`、`authentication`、`clientsecret`、`cookie`、`credential`、`password`、`privatekey`、`secret`、`signature`、`token`，或以 `auth`、`sig` 结尾，就必须改用 `query_auth` 或 `DLR-Auth`。`author`、`design`、`page`、`filter`、`X-Trace-ID` 等非敏感业务名称仍可使用；
- REST 单次请求先脱敏，再对规范化 response 执行 `max_response_bytes` 输出预算。短 Secret 使用不超过原 Secret UTF-8 字节数的星号标记；Secret 达到 10 字节时使用 `<redacted>`，脱敏不会因替换标记自身放大短 Secret；
- REST 分页只按整页提交记录。每次成功响应才计入 `pages`；请求前因总字节或时限耗尽而停止时不会虚增页数。若当前页超过剩余 `max_records`，或整页脱敏后加入 `records` 会超过 `max_bytes` 输出预算，该页完全不进入 Output；page/offset checkpoint 分别以 `start_page`/`start_offset` 指向当前未提交页边界，可叠加到原输入直接恢复。若一个单页本身超过上限，必须减小 `page_size` 或提高上限后再恢复；
- cursor 与 next URL 按可能携带凭据的不透明 continuation 处理。partial Output 的 `checkpoint` 为 `null`，不会输出原值或不可恢复的脱敏占位符，也不宣称可直接恢复；
- 页面和 Recipe 的 URL 检查不是平台级 SSRF 或出网隔离。

DLR 当前仍采用可信管理员代码模型。Recipe 级同源检查、超时和上限不能替代网络防火墙、DNS 控制、代理策略或租户隔离。

## 云与 CMDB

7 个云或 CMDB Scenario 支持：

- `preview`：只读取来源，返回 `dlr-asset-snapshot/v1` 有界资产与关系；
- `sync`：按 begin、资产批次、关系批次、finish 顺序调用外部窄合同，并只返回摘要。

sync 的 `scan_id` 与 `source_scope` 必须由调用者写入不可变 Execution Input。同一逻辑 Execution 的所有 Attempt 复用它们；代码不得随机生成替代值。缺失时必须在第一笔目标写入前失败。

任何来源范围或批次失败都必须：

- 标记 `partial=true`；
- 返回有界失败范围和 checkpoint；
- 跳过 finish；
- 不触发旧资产失效。

云账号仅建议最小只读权限，不建议 Root、主账号或写权限。关系只来自公开响应中的直接标识，不根据名称、标签或产品常识推断。

ServiceNow 三种语言 Variant 要求 `max_bytes >= 1024`，并用该值分别限制整次采集累计的原始 Table API 响应字节以及最终序列化 preview/sync envelope（包括 failures 和 checkpoint）。`instance_id`、`scan_id` 和 `source_scope` 均最长 128 字符，不能借助标识符或规范化资产绕过输出字节上限。页内遇到无效记录、`max_records` 或输出字节边界时，`checkpoint.offset` 精确指向第一条未处理的来源记录，不会按整页跨过后续行。这些 partial 路径不执行目标 begin/upsert/finish；sync 绝不在不完整扫描上 finish。

阿里云 3 个 Scenario 当前通过 Alibaba Cloud SDK `callApi` 读取来源，尚不能从目录源码证明 SDK 传输层响应受字节上限约束。因此它们的 `max_bytes` 只约束规范化输出与确定性 fixture 处理，不代表源 HTTP 响应有界；所有相关 Variant 继续保持 `reference-generated`，在升级成熟度前必须补充可审计的传输上限和真实 fixture 证据。

## 文件与数据

- CSV 可直接从普通 Execution Input 读取；Managed Input Store 关闭不影响浏览、复制和直接内容运行。
- Excel 接受 XLSX 与旧版 OLE XLS。XLSX 在调用工作簿解析器前检查 ZIP member、宏/嵌入/ActiveX 标记和外部 relationship；加密、活动内容、外链或超限 XLSX 均 fail closed。
- XLS 使用三语言离线 data-only 解析路径，不调用公式求值器、不执行宏或访问外链。它无法获得与 OOXML 等价的完整活动内容预检，因此只承诺“不执行”，不声称“已检测并拒绝”，并保持 `reference-generated`。
- 同一份真实 XLSX 已通过三种固定依赖完成窄范围一致性验证；真实旧式 XLS fixture 和完整成熟度门禁仍未完成，因此不提升标签。
- JSON 映射只支持 RFC 6901 Pointer、有限类型转换、等值或存在性过滤、稳定排序和保留首项去重，不执行 JSONata、JQ 或任意表达式。

## 数据库

PostgreSQL 和 MySQL Recipe 只允许一条参数化 SELECT：

- 必须使用数据库侧只读账号和只读事务；
- 拒绝第二条语句、INSERT、UPDATE、DELETE、DDL 和存储过程；
- 参数值通过驱动绑定，未验证输入不得拼接为标识符；
- MySQL 驱动必须关闭 multi-statements；
- 超时或失败后可靠回滚并关闭连接。

客户端 SQL 检查只是提前失败体验，不能替代数据库授权。

## 存储与传输

S3 Compatible：

- 限制页数、对象数、单对象原始字节和最终紧凑 JSON 的 UTF-8 总字节；`max_total_bytes` 至少为 256；
- 清单元数据、状态、summary、checkpoint 和 base64 放大后的内容全部计入同一个最终输出预算；每个对象的元数据与可选内容原子加入，不能只加入一半；
- 超大对象只在元数据与明确的 `limit_exceeded` 状态能完整放入预算时才返回；
- 页内停止时，checkpoint 以请求页的 `continuation_token` 和第一条未处理对象的 `object_offset` 精确恢复，不跳过同页剩余对象；
- 没有受控 Artifact 合同时，不宣称支持任意大对象落地；
- endpoint 必须是管理员信任的兼容服务。

SFTP：

- 认证前必须验证 known_hosts 或固定主机指纹；
- 使用服务端 realpath；
- 用户路径、列表项与符号链接解析后必须位于远端 base directory 内；
- 限制文件数、单文件原始字节和最终紧凑 JSON 的 UTF-8 总字节；`max_total_bytes` 至少为 256，base64、路径、清单元数据、状态、summary 与 checkpoint 均计入；
- 每个文件的元数据与可选内容原子加入；`checkpoint.start_at` 是第一条未处理相对路径的不透明游标，恢复时必须精确找到它，找不到则 fail closed，不按局部字典序跨过未处理项；
- 不默认接受未知主机密钥。

## Managed Input Store

`DLR_MANAGED_FILES_ENABLED=false` 时：

- 5/17/51 目录仍可搜索和筛选；
- 所有详情和三语言源码仍可读取；
- CSV 与 Excel 仍可复制；
- 复制不会创建文件、Artifact、Lease 或绑定；
- 用户可直接提供受限输入，或在部署以后启用能力后自行配置。

## Logo 与商标

17 个 `logo_key` 仅映射 Web 内由现有 Ant Design 图标、几何形状和受控厂家色组成的 DLR 原创 tile。它们不是厂商官方商标，不从远端加载，也不新增图标依赖。厂商名称仅用于指明兼容目标；文字标题、厂家和类型始终是可访问名称，图形本身为装饰。

## 非目标

本变更不提供：

- 用户上传、发布、售卖、评分或评论模板；
- DAG、Pipeline、CDC 或通用 Sink；
- 云资源写入；
- 自动创建 Worker、安装 Dependency 或绑定 Credential；
- 平台级 SSRF、出网隔离或不可信租户代码执行保证；
- 对全部外部服务的 live 验证承诺。
