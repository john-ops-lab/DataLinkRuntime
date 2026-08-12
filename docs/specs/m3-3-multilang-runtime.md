# M3.3 多语言 Runtime Spec

## 范围

DLR 正式支持 `python`、`javascript`、`java`。这三个 Runtime 共用 M3.2 的
Adapter、不可变 Version、Execution、生产 Worker、Publish 门禁、Start/Stop、Secret、
实时日志、timeout 与 cancel 语义；Control 永不执行用户代码。

## 语言与入口合同

- Adapter 创建时必须确定语言，创建后 API 不提供修改字段；Clone 继承源语言。
- Python：`def handle(context, input)`。
- JavaScript：ESM `export async function handle(context, input)`；同步值与 Promise 均有效。
- Java：单个 `Adapter.java`，固定 public 类 `Adapter`，入口为
  `public Object handle(Context context, Object input) throws Exception`。
- `context.config`、`context.secrets.get(key)`、`context.logger` 在三种语言中逻辑一致。
- input 必须是 JSON-compatible，output 必须可 JSON 序列化；大字段策略不按语言分叉。

## 依赖声明与环境

`AdapterVersion.requirements` 保持字段名与不可变语义，按语言解释：

| language | 格式 | Version 环境 |
|---|---|---|
| python | requirements.txt，一行一个声明 | `.venv/` |
| javascript | `package@version`，支持 `@scope/pkg@version` | `node_modules/` + 最小 package.json |
| java | `groupId:artifactId:version` | `deps/` + `classes/` + 最小 pom.xml |

空行和以 `#` 开头的注释允许。环境首次执行惰性准备，只有依赖安装与（Java）编译完整
成功后才写 `.ready`。先使用本地 cache/repository，缺失时使用对应 `pypi/npm/maven`
默认依赖源；未配置时给出管理员可读失败。Test 与 Production 使用相同路径。

## Worker 调度

- Worker 根据实际存在的 `python+uv`、`node+npm`、`java+javac+mvn` 注册 capability。
- PATCH production_worker_id、Test、Start 与 Worker claim 都验证 Adapter.language。
- 只有一个在线兼容 Worker 时自动选择；多个兼容 Worker 时要求管理员明确选择。
- TaskPayload.language 只从持久化的 Adapter.language 生成，客户端不能临时指定。

## 安全与可观测性

- 三语言均为 trusted-code model，不声明沙箱。
- 每次 Execution 都启动全新子进程/JVM，并共用 process-group kill。
- Secret 仅作为 `DLR_SECRET_*` 注入子进程，日志、错误与 Output 在持久化前脱敏。
- 依赖源认证信息不得进入 Version、Execution 日志或 Output。

## 明确不做

TypeScript、Kotlin/Groovy、任意 package.json/pom.xml/Gradle、多文件 Java 工程、自定义
Runtime 版本、Adapter 自定义镜像、常驻进程、Runner Plugin/SPI、Schedule、Webhook、
Worker Group、自动故障转移和 AI Editor 均不属于 M3.3。
