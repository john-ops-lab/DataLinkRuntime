# Issue #213：B1 稳定边界与资源准备

**结论：B1 完成，默认仍使用 LegacyRuntime，进入 B2 的 SDK/协议接入。** B0 提交 `8654f73` 的五项 CI 已通过；本批精确 HEAD 的 CI 另行跟踪。本批没有新增运行开关、接入生产 Agent、修改依赖锁、迁移数据或部署应用。

## 边界与机制清单

| 职责 | 本批位置与行为 | 后续处理 |
|---|---|---|
| 领域预算、知识证据、严格 JSON/候选与完成判定 | `ai/policy.py`；旧服务保留兼容导入，新旧运行器共用。两类状态及三个解析/候选函数的 AST 与提取前一致，默认计数、UTF-8 result_size 与停止优先级保持 | 保留业务策略 |
| Provider 配置与准确能力判断 | `ai/provider_config.py`；19 项原定义 AST 保持，另加不可变配置/凭据快照，配置数据格式不变 | 正式 SDK 工厂复用 |
| 模型目录与草稿连接测试 | `ai/provider_catalog.py`；职责独立，暂沿用原受限传输和协议辅助函数，原 `providers` 导出保持 | B2/B5 逐项复核，不删除目录能力 |
| 内部运行接口 | `runtime/contracts.py`、`legacy.py`；请求 JSON 冻结，输出组合现有领域 Schema，框架对象不进入 API、领域策略或 Worker | B3 接新运行器，B5 删除旧实现 |
| 执行线程资源 | `runtime/resources.py`；Session 在实际执行线程创建/关闭，每次工具获得独立上下文与 Reader context | B3 的工作所有者管理其实际结束 |
| 旧 Assist 循环与协议编解码 | 仍在旧服务/`providers.py`，没有仅因移动文件宣称删除或净减负 | B3 接管、B5 按退出条件删除 |

API 冻结结果：116 个路径，完整 OpenAPI SHA-256 仍为 `aa97398505a4cbd26050a18b2c265df1007bb276953c6a3dcff5fca4cccef2f4`。请求/响应字段、候选预览/应用流程、模型配置与五语言 Prompt 契约保持。

## 配置、权限与文件生命周期

- 模型配置/凭据和知识源配置按单个请求冻结，不把 ORM 对象传入工具线程；知识源快照不持有 Session，Secret 字段不进入 repr。真实 PostgreSQL 验证：工具会话能看到已提交的新凭据，当前请求仍使用旧快照，下一请求使用新快照。
- Session 工厂只捕获线程安全 Engine；每个工具在实际执行线程创建并关闭短 Session。新增集成断言核对工具没有使用请求 Session，Session 创建线程与 handler 线程相同，退出后已关闭。
- API 用冻结的 owner/adapter/scope/ref/hash/revision 创建 Reader 工厂。外层准备 pin 与工具工作 pin 分别拥有：集成测试观察 `0→1→2→1→0`，scope 关闭及旧失败结果不会覆盖冻结身份。
- 权限复查用新会话，读取中及返回前可见撤权/归档；普通咨询现在也经过返回前复查。真实 API 验证上游返回前归档 Adapter 后返回既有 `adapter_not_found`，未增加 Provider 请求。

## 相称验证与首次失败

| 验证 | 结果与归属 |
|---|---|
| 共享策略/接口提取首轮 | 367 passed；包含设置、严格输出、工具、知识、预算、deadline、容量和附件完成 |
| 资源接入首轮 | 339 passed、39 failed；纯领域夹具用 `session=None` 且旧 API stub 未声明新增资源工厂参数。保留首次输出，未删除领域断言或修改产品以容忍无 Session |
| 资源针对性复测 | 49 passed、1 failed；新增全 overview 对比因 TTL 动态元数据不同失败。改为核对冻结 ref/hash/revision，并保留所有 pin 与内容断言 |
| 完成/真实 Reader/资源集成 | 上述唯一失败对应用例复测通过；其余 49 已通过。覆盖原完成判定、缓存 ACL、真实短 Session、凭据冻结和普通咨询返回前授权 |
| 当前 Prompt/五语言输入/托管输入/审计/诊断 | 233 passed，包含唯一 Reader 复测；#215 当前注释语言及候选配置限制保持，未把历史真实模型 FAIL 改写为通过 |
| Catalog/设置/协议入口迁移 | 102 passed、79 deselected；限定设置、模型、Provider、连接测试，原传输 mock seam 保持 |
| B0 新旧领域/协议代表回归 | 110 passed，含真实专用 PostgreSQL；历史三项薄包装 gap 仍保留 |
| 静态与契约 | 全后端严格 Mypy 189 个源文件、Ruff 检查/386 文件格式、OpenAPI hash、diff 检查通过；OpenSpec 严格校验归属于本批提交 |

资源夹具改为注入无数据库的测试资源工厂，独立 PostgreSQL 集成测试继续证明生产工厂的 Session/Reader 所有权。首次失败与后续结果分开保留；没有把夹具问题登记成模型质量失败。

G01/G02 尚未在生产路径实现，G03/G04 扩展对照及 G06 最终收益仍待 B3–B5。B1 是职责提取与请求资源准备，不能以文件移动或测试通过宣称迁移完成。
