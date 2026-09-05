## Why

DLR 目前要求用户从空白代码开始创建 Adapter，缺少按业务目的发现、比较并安全复制的官方起点。Issue #130 已合并到 `main`，用户也已批准“适配器 / 模板广场”一级导航设计并明确授权 #132，因此现在可以在不改变 Runtime 权威边界的前提下交付可追溯的模板资产与受控实例化流程。

## What Changes

- 新增由仓库版本化维护的模板目录，固定首期 5 个主题、17 个场景和 51 个 Python / JavaScript / Java 语言变体；模板是静态 Recipe，不是隐藏或可执行的系统 Adapter。
- 新增认证后只读的主题、场景列表、筛选、分页、场景详情和按语言延迟加载接口。列表只返回摘要与语言可用性，不携带三份完整代码。
- 新增独立的模板实例化事务：用户选择语言并填写名称后，一次性创建归当前用户所有、默认停止的 Adapter 与 Revision 1；不得复制或绑定 Credential、Dependency、Managed File、Worker、Schedule/Webhook 启用状态或历史。成功响应返回新 Adapter，Web 自动进入该 Adapter 的编辑页。
- 在 Adapter 上保存可选、只读的 `template_scenario_slug` 与 `template_version` 来源元数据；模板之后升级不得回写或覆盖用户 Adapter。
- 新增顶栏一级导航 `适配器 / 模板广场`。模板广场使用完整主内容区呈现主题、搜索、筛选、场景卡片、分页和详情；Adapter Catalog 的“新建”流程可提供“从模板创建”辅助入口，但不代替一级菜单。
- 为模板卡片和详情提供按厂商或场景类型映射的本地矢量 Logo。Logo 必须美观、一致、可离线加载；外部品牌图形仅在来源和许可可审计时纳入，否则使用 DLR 自有图形，不从远端热链资源。
- 为 17 个场景冻结输入、输出、依赖、Runtime 建议、安全边界和三语言行为合同。云资源 / CMDB 场景提供有界 `preview` 与面向窄 CMDB Upsert 合同的幂等 `sync`；转换和 Webhook 场景不伪造 `sync`。
- 建立“资源或行为 → 固定来源 commit/tag → 文件/API → 分页/关系 → 许可证处理 → 核对日期”矩阵。GPL、ELv2、无许可证或不兼容来源只用于行为研究；可改编来源保留所需 NOTICE，所有代码均接受 Secret/真实 Endpoint/本机路径扫描。
- 分语言记录 `reference-generated / syntax-verified / fixture-verified / live-verified` 成熟度；未实际执行的验证不得升级标签。默认关闭 Managed Input Store 时，所有模板仍可浏览、查看和复制。
- 增加 Backend/Web/数据库迁移、三语言静态或 fixture 校验、浏览器视觉与交互、国际化、可访问性、安全和许可证测试，并同步中英文产品/架构/使用文档。
- **非目标**：不建设用户发布、上传、售卖、评分或评论市场；不建设 DAG、通用 Pipeline/Sink、CDC、平台级 SSRF/出网隔离或云资源写操作；不要求所有外部服务都达到 `live-verified`。

## Capabilities

### New Capabilities

- `template-catalog-api`: 定义版本化模板目录、5/17/51 清单、搜索筛选分页、详情/语言延迟加载、Logo key、来源与成熟度的只读 API 合同。
- `template-adapter-instantiation`: 定义模板到用户 Adapter + Revision 1 的原子实例化、名称/权限/运行态隔离、来源审计与成功后编辑页交接合同。
- `template-gallery-web`: 定义全局一级导航、主题/筛选/卡片/详情/Logo、语言切换、响应式/可访问性和实例化后自动进入编辑页的 Web 行为。
- `template-recipe-library`: 定义 17 个场景、51 个语言变体、通用输入输出、安全限制、依赖建议、来源许可证、成熟度以及云 / CMDB `preview`/`sync` 行为。

### Modified Capabilities

无。模板必须服从现有 Adapter、统一输入、Reliable Runtime、Credential 与 Managed Input 合同，但本变更不放宽或改写这些权威要求。

## Impact

- Backend：新增模板 manifest/代码资产加载与校验、只读查询/实例化 schema、service 和 API；`adapters` 增加两个 nullable 来源字段，Alembic 使用 additive migration。模板静态资产随 Control wheel/容器发布，不引入运行时远程下载。
- Web：扩展 `ApplicationShell` 全局路由和 dirty-leave 防线，新增 Gallery/Detail/Instantiate 组件、模板 API/types、zh-CN/en 资源、本地 Logo 资产与浏览器测试；React 19、Ant Design 5.29.3、ProComponents 2.8.10 版本保持不变。
- Runtime：模板本身不进入 Execution、Outbox、Attempt、Slot 或 Worker；复制出的普通 Adapter 仍按 #130 当前合同保存和执行。模板实例化不自动选择 Worker、安装依赖或绑定 Secret。
- 数据与兼容：新字段均可空，旧 Adapter 和旧客户端保持兼容；回滚 UI/API 不删除已实例化 Adapter。旧二进制可忽略 additive 字段，生产恢复不依赖破坏性 downgrade；若执行 downgrade，只会丢失可选来源审计字段，不改变 Adapter Revision 内容。
- 安全与许可证：API 仅返回公开模板资产和允许的来源元数据；不得返回真实 Credential、Endpoint、路径或运行态对象。第三方参考和 Logo 需要固定版本、许可分类、NOTICE/归属与公开文件扫描。
- 交付：一个 `codex/issue132-template-gallery` 分支和一个 Draft PR；实现、测试、Hosted CI、Review、合并、发布与用户人工验收保持为不同事实，本变更不自行合并或关闭 #132。
