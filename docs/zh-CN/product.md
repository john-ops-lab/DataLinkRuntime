# DLR（DataLinkRuntime）产品定义

> 当前源码基线包含 Issue #130 Reliable Runtime；发布、Hosted CI、独立 Review 与用户验收仍是分开的状态。
> 本文档描述当前已经实现的产品模型；历史决策见 `docs/specs/README.md`、历史 Specs 与数据库迁移记录。新任务的目标以当前明确授权的 GitHub Issue 为准。

## 1. 产品定位

DLR 是一个轻量的数据适配运行平台，用于 CMDB 等系统的数据采集、接收、解析、转换和输出。

核心目标：

- 一台服务器与 Docker Compose 即可部署完整平台；
- 在浏览器中完成 Adapter 创建、编辑、保存、运行、停止、Clone 升级与排障；
- Python、JavaScript、Java 使用一致的 input / output / log 体验；
- AI Assistant 只产生候选修改，最终保存和运行始终由管理员明确执行；
- 不发展成工作流引擎、低代码平台或通用插件平台。

## 2. 核心对象

| 对象 | 当前定义 |
|------|----------|
| Adapter | 一个独立的数据处理单元，类型为 Task 或 Webhook |
| Revision | 每次保存产生的不可变代码、依赖和运行参数快照；属于内部审计事实 |
| Execution | 一次逻辑执行，固定 input、Revision、目标 Worker、backend、generation、状态与结果 |
| Attempt / Slot | RabbitMQ Execution 的一次实际执行，以及每个 Adapter `Slot 0` 的并发权威 |
| Worker | 实际运行用户代码的节点，按语言、协议和 isolation capability 参与调度 |
| Credential | 加密保存的凭据；浏览器永远拿不到真值 |

用户只执行“保存”。系统在后台创建不可变 Revision，并让后续运行固定使用最新已保存内容。

### 2.1 模板广场与 Recipe 实例化

模板广场是与“适配器”并列的一级入口。首期目录随 DLR 版本静态发布，精确包含 5 个
Theme、17 个 Scenario，以及每个场景各一份 Python、JavaScript、Java 实现，共 51 个
Variant。用户可以按主题、关键词、厂家、Adapter 类型、协议、语言和成熟度查找场景；
详情只在选定语言后加载该 Variant 的代码、合同、依赖建议和来源。

用户选择语言、填写名称并确认后，系统在一个事务中创建独立 Adapter、Slot 0、类型所需
的最小禁用配置和 Revision 1，然后 Web 自动进入新 Adapter 的编辑页。它不是普通 Clone：
新 Adapter 默认停止且没有 Worker、Credential Binding、已安装 Dependency、Schedule、
Managed File、Execution 或历史；模板后续升级不会回写用户代码。用户必须在运行前自行
审阅和修改代码、选择兼容 Worker、安装精确依赖并完成输入配置。

Recipe 的非敏感参数进入 `context.config` 或 Execution Input；密码、Token、私钥等值只
能通过 Credential Binding 注入 `context.secrets`。外部 Endpoint 是管理员审核的运行
配置，不得携带认证 Query，也不会因复制模板自动获得可信身份。Recipe 的 HTTPS、同源
跳转、超时和上限检查不构成平台级 SSRF 或出网隔离；生产部署仍需用网络策略限制 Worker
可访问的地址。

7 个云与 CMDB Scenario 提供只读 `preview`；其规范化结果和 Adapter Output 受页数、
记录数、字节数与请求总时限约束。可选 `sync` 面向外部 `dlr-cmdb-upsert/v1` 目标合同，
必须从不可变 Execution Input 获得稳定的 `scan_id` 与 `source_scope`；同一次逻辑 Execution
的所有 Attempt 必须复用二者，任一来源或批次失败时不得调用 finish。阿里云 3 个 Scenario
使用的 Alibaba Cloud SDK `callApi` 尚不能证明原始传输响应受字节上限约束，因此这里的
有界承诺不包含其源 HTTP 响应。下面只是身份字段片段，不代替具体 Variant 的完整输入合同：

```json
{
  "mode": "sync",
  "scan_id": "123e4567-e89b-42d3-a456-426614174000",
  "source_scope": "alicloud:EXAMPLE_ACCOUNT:example-region-1"
}
```

上面的 UUID 仅为可复制的匿名示例；新的业务扫描应换用新值，同一业务扫描的重试必须复用
原值。

`DLR_MANAGED_FILES_ENABLED=false` 不影响 5/17/51 目录的浏览、代码查看或复制，包括 CSV
与 Excel；复制也不会创建文件、Artifact、Lease 或绑定。运行时如何提供内容或文件，应
以所选 Variant 的输入合同和部署能力为准。

成熟度按 `scenario + version + language + source_sha256` 独立展示：
`reference-generated / syntax-verified / fixture-verified / live-verified`。每个标签由匹配
源码哈希的 Receipt 约束；`reference-generated` 表示尚无满足下一等级全部门禁且匹配当前
源码哈希的 Receipt，允许存在不构成升级证据的窄 smoke 或安全 canary。语法/编译通过只可
证明 `syntax-verified`，不能冒充完整 fixture 或真实外部服务。详细操作与边界见
[Template Recipe 使用与安全边界](../templates/recipe-usage-security.md)、
[CMDB Upsert v1 合同](../templates/cmdb-upsert-v1.md)和
[成熟度与 Receipt](../templates/maturity-receipts.md)。

## 3. Adapter 类型

### 3.1 Task Adapter

Task 支持两种运行方式：

- 手动运行：`运行一次` / `停止运行`；
- 定时运行：配置 Cron、Timezone 与 Input，使用 `启用定时` / `停用定时`，并可 `立即运行一次`。

页面信息架构固定为：

```text
编辑
运行设置
执行记录
```

### 3.2 Webhook Adapter

Webhook 由 Control 提供统一入口：

```text
POST /api/hooks/{public_id}
Authorization: Bearer <token>
```

用户配置可读 path、Token Credential 与运行节点，并使用 `开启接收` / `停止接收`。
请求通过校验和 Admission 后异步创建 Execution 并返回 `202 + execution_id`。RabbitMQ
backend 可保留多个不可变 `queued/retry_wait` Execution，但同一 Adapter 仍只有一个
active Attempt；legacy backend 在兼容期继续使用原单活跃门禁。

页面信息架构固定为：

```text
编辑
运行设置
调用记录
```

Webhook、Task 与 Schedule 的终态 Execution 统一按部署配置的保存天数和每个
Adapter 数量上限分批清理；`pending` / `running` 永不由 retention 删除。具体默认值、
批量大小和平台服务日志轮转规则见 `docs/deployment/platform-logs.md`。

## 4. 保存与运行节点

- 页面只显示“保存”；
- 第一次保存必须确定有效在线且语言兼容的运行节点；
- 只有一个兼容节点时可自动选择；多个节点时由用户明确选择；
- 后续保存沿用当前运行节点，可在运行设置中查看或修改；
- 所有运行入口都使用最新已保存内容与 Adapter 当前运行节点。

## 5. 统一运行锁

legacy backend 的一个 Adapter 同时最多一个 `pending/running` Execution。RabbitMQ
backend 允许有界排队，但数据库 `Slot 0` 同时最多绑定一个 active Attempt。Task
手动运行、Schedule 与 Webhook 共用相同的 Admission、快照和 Slot 规则。

Schedule/Webhook 已启用、存在 legacy active Execution，或存在 RabbitMQ active Attempt
时禁止：

- 修改代码、依赖、运行参数、Credential binding；
- 修改 Worker、Task 运行方式、Cron、Webhook path 或 Token；
- 保存与删除 Adapter。

纯 `queued/retry_wait` 已固定自己的不可变快照，不会单独锁住当前 InputConfig；后续
保存只影响新 Execution。名称和描述仍可修改。前端必须解释禁用原因，后端继续以
稳定 409 错误作为最终门禁。

## 6. 实时日志与历史

所有触发方式复用 Execution SSE 与同一个 watcher：

- Task 点击运行后自动展开页面底部实时日志；
- Schedule 新 Execution 开始时提示并进入对应日志，不切走用户正在查看的历史详情；
- Webhook 开启后显示“等待 Webhook 请求…”，真实请求创建 Execution 后自动跟踪；
- 日志工作区支持底部、全屏和恢复到底部；
- Execution 终态后仍可从执行记录或调用记录查看完整详情。

## 7. Clone 升级

运行中升级使用 Clone：

```text
复制 Adapter
→ 修改并保存新 Adapter
→ 可选验证
→ 停止旧 Adapter
→ 运行新 Adapter
→ 验证后删除旧 Adapter
```

Clone 复制语言、代码、依赖、运行参数、Credential 引用、触发配置和运行节点；不复制 Execution 历史，新 Adapter 始终保持停止。

Webhook Clone 可以与源 Adapter 使用相同 path，但同一时刻只有一个可以开启接收，因此外部 URL 可在人工停旧、启新的步骤中保持不变。

## 8. 删除

用户操作统一为“删除 Adapter”。运行中必须先停止；删除成功后 Adapter 从活跃 Catalog 消失。后台保留软删除事实，为未来独立设计回收站能力留下边界，但当前产品不提供回收站入口。

## 9. Runtime Contract

三种语言共享：

```text
Input → handle(context, input) → Output
```

- Python：`def handle(context, input)`；
- JavaScript：`export async function handle(context, input)`；
- Java：固定 `Adapter` 类与 `handle(Context context, Object input)`；
- `context.config` 提供非敏感运行参数；
- `context.secrets.get(key)` 提供绑定凭据；
- `context.logger` 输出实时日志。

RabbitMQ v3 路径的状态包括 `queued / running / retry_wait / dead_letter` 与既有终态。
Worker 在 Control durable Claim commit 和私有 journal 原子落盘后 ACK，再进入资源
Sandbox；ACK 后崩溃由 Attempt Lease/Fencing 与新 generation 恢复，不能依赖原消息
重投。Adapter 对外部系统产生的副作用仍应使用业务幂等键。

Task Starter Code 输出“任务开始 / 任务结束”；Webhook Starter Code 输出“收到 Webhook 请求 / 处理完 Webhook 请求”。新建 Adapter 时 Starter Code 的注释与示例平台日志跟随当前系统语言（`zh-CN` 中文 / `en` 英文）；已存在 Adapter 的代码不因切换语言而改写，也不创建新 Revision。

## 10. AI Assistant 边界

AI Assistant 可以读取当前 Working Copy 和最小非敏感上下文，返回完整 Candidate 并提供 Diff。一次性附件支持 PDF、DOCX、XLS、XLSX、文本、代码与受支持图片；表格只在内存中读取有界单元格显示值，不执行公式、宏或外链。当前 Managed Input 只向 AI 暴露排序后的文件名、类型等安全标签和三语言 `context.input_files` / `context.inputFiles` 指引，AI 不读取 Blob，也不得声称看见未作为附件上传的文件内容。Apply 只更新浏览器 Working Copy，不保存、不运行、不修改 Credential 真值或运行状态。Prompt、Provider 原始响应、reasoning、附件正文与对话不落库。

## 11. 安全原则

- Adapter 代码不得硬编码密码、Token 或私钥；
- Credential 真值不返回浏览器、不写日志、不进入 AI Prompt；
- Webhook Bearer Token 使用 constant-time compare；
- Runtime 日志按已注入 Secret 集合脱敏；
- DLR 仍是可信管理员代码模型；Linux cgroup v2 Sandbox 约束资源和进程，但不构成
  面向不可信租户的安全边界；
- 默认单节点 RabbitMQ 不是 HA，Quorum Queue 持久化不能替代多节点容灾。

## 12. 系统语言与显示名称

系统设置中的「语言」是部署级系统语言，默认 `zh-CN`，可切换为 `en`：

- 由管理员修改并持久化为认证态权威值，控制台与 Ant Design 内置文案同步跟随；未认证登录页使用独立浏览器偏好（首次 `zh-CN`），认证成功后立即恢复系统语言；
- 新建 Execution 时捕获当时的系统语言，并在该 Execution 生命周期内固定，不因运行中切换而改变；
- 后端稳定错误以 `error code + structured params` 为机器合同，前端按当前语言本地化展示，现有 message 兼容保留；
- 系统预置内容（依赖源、Credential 类型等）按语言显示本地化名称，内部 code / ID 不变，业务判断不依赖显示名称；
- 用户创建的 Adapter 名称、描述、Credential 名称等不自动翻译；
- 切换语言不修改已有代码、不创建新 Revision、不批量改写历史 Execution 日志。

## 13. 当前不做

不实现 Adapter 串联、DAG、同步 Webhook invoke、URL takeover、常驻 Adapter、RBAC、
AI 自动执行循环、通用插件框架、统一 Sink、用户级语言偏好、机器自动翻译用户内容、
第三语言或 RabbitMQ 集群 HA。Reliable Runtime 的重试是单个 Execution 的有界恢复
合同，不是通用工作流编排。

## 14. 当前完成判据

不了解 DLR 内部 Revision 实现的技术用户，可以不查文档完成：

```text
Task / Webhook 创建
→ 编辑与保存
→ 选择运行节点
→ 运行或开启接收
→ 查看实时日志与历史
→ 停止
→ Clone 升级
→ 删除旧 Adapter
```

也可以从模板开始：

```text
模板广场选择场景与语言
→ 复制并命名
→ 自动进入独立 Adapter 编辑页
→ 配置 Worker / Dependency / Input / Credential / Endpoint
→ 保存、预览并按成熟度证据决定是否投入使用
```

## 15. Reliable Runtime 运维边界

- 默认部署保持 RabbitMQ 普通 ingress 关闭、legacy Claim 开启和三个 Cutover attestation
  关闭；普通安装不会自动进入最终切换。
- Final Cutover 是管理员分阶段操作：备份恢复实测、legacy drain/migrate、Worker v3 +
  Linux Sandbox、普通流量、Slot 压力、minimum protocol 3、退役旧索引，最后关闭
  legacy Claim。顺序不可交换。
- Cutover 后的回滚使用理解 additive schema 的兼容 Control drain/repair。不得启动旧
  二进制解释新 row，也不得把生产 `alembic downgrade` 当作恢复方案。
- 详细配置、只读 inventory/preflight/invariant API 和故障处理见
  [Reliable Runtime 迁移说明](issue130-reliable-runtime-migrations.md)；Linux 部署前置见
  [Sandbox 部署说明](issue130-sandbox-deployment.md)。
