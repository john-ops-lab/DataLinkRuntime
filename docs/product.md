# DLR（DataLinkRuntime）产品定义

> 版本：v1.0（已确认）
> 本文档范围：产品定位、核心概念、行为边界与阶段目标。
> 技术实现见 [architecture.md](./architecture.md)；工程规范见 `.qoder/rules/engineering.md`，此处不重复。

## 1. 产品定位

DLR 是一个**轻量的数据适配运行平台**，主要用于 CMDB 等系统的数据采集、接收、解析、转换和输出。

核心目标：

- 快速开发：Adapter 从编写到可运行的路径尽可能短。
- 部署简单：一台服务器 + Docker Compose 即可运行完整平台。
- 运维简单：组件少、依赖少、可观测性内建。
- 用户可在线编辑 Adapter 代码（Web IDE 体验，Monaco Editor）。
- 支持 AI 理解当前 Working Copy、生成或修改候选代码，并由管理员查看 Diff 后人工应用。
- 支持查看测试输入、输出和运行日志。
- 支持多种触发方式（第一阶段仅 Manual）。
- 保持轻量：**不发展成工作流平台**。

## 2. 核心对象：Adapter

平台中的核心数据处理单元统一称为 **Adapter**。

一个 Adapter 独立完成一次完整的数据处理职责：

```
外部数据输入 / 主动采集
→ 业务逻辑
→ 解析和转换
→ 输出 KV / JSON
→ CMDB 或其他目标系统
```

原则：

- 一个业务处理过程由一个 Adapter 完成。
- 逻辑复杂时，把业务逻辑写在 Adapter 内部，而不是拆成多个 Adapter 接力。
- **向目标系统的输出由 Adapter 代码自行完成**（如调用 CMDB API）。平台不代替 Adapter 完成对外数据输出，不提供统一 Sink / Connector 框架。

明确不设计：

- Adapter → Adapter 串联
- DAG / Workflow Engine / 可视化编排

## 3. 核心领域概念

| 概念 | 定义 | v1 状态 |
|------|------|---------|
| Adapter | 一个逻辑数据处理单元 | 实现 |
| Adapter Version | Adapter 的某一个已保存版本，包括代码、运行时配置和依赖声明；不可变 | 实现 |
| Adapter Instance | Adapter 部署到 Worker 后实际运行的实例（常驻语义） | 仅保留长期概念，v1 不实现 |
| Execution | Adapter 的一次具体执行记录 | 实现 |
| Control Node | 平台管理节点 | 实现 |
| Worker Node | 真正运行 Adapter 的工作节点 | 实现 |

Execution 包含：input、output、stdout、stderr、start time、end time、status、duration。

## 4. 执行模型

- v1 只支持 **One-shot（一次性）Adapter**：每次触发 = 一次完整执行 = 一条 Execution 记录。
- 常驻类型 Adapter（长生命周期进程）属于长期方向，v1 不建表、不实现进程生命周期管理。
- **所有用户代码执行（包括 Manual Test）都在 Worker 上进行**，Control Node 不运行用户代码。

## 5. Trigger

| 类型 | 语义 | 阶段 |
|------|------|------|
| Manual | 用户手工点击执行一次 | **v1 实现** |
| Schedule | 按配置的时间周期执行 | 后续 |
| HTTP / Webhook | 外部系统调用平台统一入口触发 | 后续 |

Webhook 的未来设计约定（已裁决）：

- 由 Control 统一入口接收并路由，**不允许每个 Adapter 暴露独立监听端口**。
- **默认异步语义**：Control 收到事件后创建 Execution 并尽快返回 `202`，Worker 后台执行。
- 不为 Webhook 设计 Control 长时间同步等待 Worker 的通道。
- 如未来确有同步调用需求，单独设计 invoke API，与 Webhook 解耦。

## 6. Adapter 开发体验

最终目标是类似轻量 Web IDE 的体验：

创建 Adapter → 选择语言 → 在线编辑代码 →（AI 生成/修改 Candidate → 人工 Apply）→ 保存 → 测试 → 查看 Input / Output / 实时 Log → 发布 → 查看执行历史。

- 代码编辑器：Monaco Editor。
- Python、JavaScript、Java 均支持 AI Assistant；Candidate 采用完整快照，不做模糊 patch apply。
- AI Apply 只写浏览器 Working Copy 并进入 dirty；Save、Test、Publish、Start、Stop 等生命周期动作仍须管理员人工执行。
- AI 会话仅存在于当前浏览器与当前 Adapter；切换 Adapter 或刷新页面后允许消失。
- 当前不实现：Schedule、Webhook、AI Agent 自动执行循环。

## 7. Runtime Contract（产品级约定）

不同语言最终拥有统一的逻辑入口语义：

```
Input → Adapter → Output
```

- Input / Output 使用 JSON-compatible / JSON-serializable 语义。
- Output 必须允许对象、对象数组等常见形式。
- 具体契约见 architecture.md 的 Runtime 章节。

## 8. 安全原则

- Adapter 代码中**不得硬编码明文** Password / Token / API Secret / SecretKey。
- Adapter 通过 Runtime Contract 的 `context.secrets.get(key)` 获取凭据。
- Secret 可来自 Worker 的 `DLR_SECRET_*` 环境变量或平台 Secret Store；平台只持久化
  Fernet 密文，claim 时按 Adapter 绑定解密并注入 Worker。
- AI Prompt / 上下文只携带已绑定业务 Secret 的 `env_key` 名称，不携带其真值、密文或
  平台 Token。模型 API Key 仅作为 Provider HTTP Authorization 使用，不进入 Prompt。
  管理员配置的 Provider / Base URL 是 Working Copy 与非敏感运行参数的外部数据边界。
- AI reasoning 不返回浏览器、不持久化、不进入下一轮对话，也不写普通应用日志；无法可靠
  分离最终回答时整次请求失败。
- v1 为内网、单管理员、可信代码模型，详见 architecture.md 安全边界章节。

## 9. 部署原则

- 最小化部署：一台服务器 + Docker Compose 运行完整 DLR。
- 逻辑组件保持独立：`web` / `control` / `postgres` / `worker`，**不合并进单个容器**。
- 未来允许 Control 与 Worker 分机部署（Control 在 A，Worker 在 B/C/D）；第一阶段只做单机。

## 10. 语言支持

- 当前正式支持：**Python、JavaScript、Java**。
- Adapter 创建时确定语言且不可修改；Clone 继承源语言，所有 Version 继承 Adapter 语言。
- Adapter 的第三方依赖属于 Adapter Version，不属于平台全局依赖；不同 Adapter 依赖相互隔离。

## 11. 明确不做的事

DLR 不是：n8n / Windmill / Kestra / DAG Engine / Workflow Engine / Kubernetes 平台 / 通用低代码平台。

当前不引入：Kubernetes、Service Mesh、Kafka、RabbitMQ、Event Bus、微服务拆分、分布式一致性方案、复杂插件系统、Adapter-to-Adapter Workflow、统一 Sink / Connector Framework、RBAC / 账号体系、AI Agent Framework、RAG / Embedding / Vector DB、多模型自动路由。

## 12. 第一阶段目标与里程碑

第一阶段打通最小闭环：

```
创建 Adapter → 选择 Python / JavaScript / Java → 在线编辑 / AI Candidate 人工 Apply → 保存 → Manual Test
→ 兼容 Worker 执行 → 查看 Log / Output → Publish → Start / Stop
```

| 里程碑 | 内容 |
|--------|------|
| M0 工程骨架 | 仓库结构、四容器 Compose、Health Check、SQLAlchemy + Alembic 骨架、基础测试、lint / type check、CI、README |
| M1 Adapter 管理 | Adapter CRUD、Monaco 在线编辑、保存即不可变版本、发布、requirements / runtime 配置 |
| M2 执行闭环 | Worker 注册 / 心跳、version-scoped venv、Manual 触发、子进程执行、Execution 落库（含大字段策略） |
| M3 可观测与体验 | 测试输入面板、Output 查看、实时日志、执行历史 |
| M3.1 Console 视觉收敛 | Catalog / Workbench / Monaco / Test / History 控制台体验 |
| M3.2 生产生命周期 | 生产 Worker、发布门禁、Start/Stop、Secret Store、依赖源 |
| M3.3 多语言 Runtime | Python / JavaScript / Java、三语言依赖环境与 capability 调度 |
| M4 AI Editor | 单一全局模型配置、三语言 AI Assist、完整 Candidate、Diff、人工 Apply、stale 防覆盖 |

M0 不实现 Adapter CRUD 与 Adapter Runtime。
