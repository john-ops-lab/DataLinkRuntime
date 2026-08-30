# DLR（DataLinkRuntime）产品定义

> 当前基线：`v0.1.1`（包含 Issue #117 手工测试问题修复）。
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
| Execution | 一次具体执行，记录 input、output、stdout、stderr、状态与耗时 |
| Worker | 实际运行用户代码的节点，按语言 capability 参与调度 |
| Credential | 加密保存的凭据；浏览器永远拿不到真值 |

用户只执行“保存”。系统在后台创建不可变 Revision，并让后续运行固定使用最新已保存内容。

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
请求通过校验后异步创建 Execution 并返回 `202 + execution_id`；同一 Adapter 忙时不排队。

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

一个 Adapter 同时最多只有一个 `pending / running` Execution。Task 手动运行、Schedule 与 Webhook 共用这一把锁。

运行中或入口已启用时禁止：

- 修改代码、依赖、运行参数、Credential binding；
- 修改 Worker、Task 运行方式、Cron、Webhook path 或 Token；
- 保存与删除 Adapter。

名称和描述仍可修改。前端必须解释禁用原因，后端继续以稳定 409 错误作为最终门禁。

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

Task Starter Code 输出“任务开始 / 任务结束”；Webhook Starter Code 输出“收到 Webhook 请求 / 处理完 Webhook 请求”。新建 Adapter 时 Starter Code 的注释与示例平台日志跟随当前系统语言（`zh-CN` 中文 / `en` 英文）；已存在 Adapter 的代码不因切换语言而改写，也不创建新 Revision。

## 10. AI Assistant 边界

AI Assistant 可以读取当前 Working Copy 和最小非敏感上下文，返回完整 Candidate 并提供 Diff。一次性附件支持 PDF、DOCX、XLS、XLSX、文本、代码与受支持图片；表格只在内存中读取有界单元格显示值，不执行公式、宏或外链。当前 Managed Input 只向 AI 暴露排序后的文件名、类型等安全标签和三语言 `context.input_files` / `context.inputFiles` 指引，AI 不读取 Blob，也不得声称看见未作为附件上传的文件内容。Apply 只更新浏览器 Working Copy，不保存、不运行、不修改 Credential 真值或运行状态。Prompt、Provider 原始响应、reasoning、附件正文与对话不落库。

## 11. 安全原则

- Adapter 代码不得硬编码密码、Token 或私钥；
- Credential 真值不返回浏览器、不写日志、不进入 AI Prompt；
- Webhook Bearer Token 使用 constant-time compare；
- Runtime 日志按已注入 Secret 集合脱敏；
- v1 是可信管理员代码模型，子进程隔离不构成安全沙箱。

## 12. 系统语言与显示名称

系统设置中的「语言」是部署级系统语言，默认 `zh-CN`，可切换为 `en`：

- 由管理员修改并持久化为认证态权威值，控制台与 Ant Design 内置文案同步跟随；未认证登录页使用独立浏览器偏好（首次 `zh-CN`），认证成功后立即恢复系统语言；
- 新建 Execution 时捕获当时的系统语言，并在该 Execution 生命周期内固定，不因运行中切换而改变；
- 后端稳定错误以 `error code + structured params` 为机器合同，前端按当前语言本地化展示，现有 message 兼容保留；
- 系统预置内容（依赖源、Credential 类型等）按语言显示本地化名称，内部 code / ID 不变，业务判断不依赖显示名称；
- 用户创建的 Adapter 名称、描述、Credential 名称等不自动翻译；
- 切换语言不修改已有代码、不创建新 Revision、不批量改写历史 Execution 日志。

## 13. 当前不做

不实现 Adapter 串联、DAG、同步 Webhook invoke、请求队列、自动重试、URL takeover、常驻 Adapter、RBAC、AI 自动执行循环、通用插件框架、统一 Sink、用户级语言偏好、机器自动翻译用户内容或第三语言。

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
