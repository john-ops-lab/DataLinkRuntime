## 1. 基线、来源与目录骨架

- [x] 1.1 重新确认 Issue #130 已合并、当前分支基于 `d28daabfe9e70a5d5db23fb62613c5c39222764b` 且根 checkout 无污染；验证：记录 `git status`、HEAD/merge-base 和 Issue/PR 状态，不匹配时停止实施。
- [ ] 1.2 建立 17 场景资源/行为覆盖矩阵，填写来源 URL、固定 SHA/tag、精确文件/API、分页、关系证据、许可证、使用方式、SDK 版本、fixture 和 `checked_at`；验证：矩阵 schema 检查全部必填列且“不支持/缺口”显式可见。
- [x] 1.3 增加模板 package 目录、`catalog.json`、`provenance.json` 和 17 个 metadata 骨架，冻结 5 Theme、17 Scenario、51 Variant、17 个 `logo_key` 与版本；验证：目录清单测试精确断言 5/17/51、slug 唯一和每场景三语言。
- [x] 1.4 增加资产快照 v1 与 CMDB Upsert v1 JSON Schema，冻结 external key、关系枚举、确定性排序/去重、summary/checkpoint 和上限字段；验证：有效/无效 fixture 均通过预期 schema 断言。
- [x] 1.5 完成许可证策略与第三方 NOTICE，仅把兼容来源标成可改编，把 GPL/ELv2/无许可证来源标成 behavior-only；验证：自动检查每条 provenance 的许可证证据与 use mode 组合并人工核对 NOTICE。

## 2. Backend 静态目录与只读 API

- [x] 2.1 实现不可变目录 schema、`importlib.resources` loader、显式资源映射、SHA-256 和 fail-closed 全量校验；验证：单元测试覆盖重复 slug、缺语言、坏 hash、未知枚举/Logo、失效交叉引用和路径穿越。
- [x] 2.2 实现 Variant 按 `(slug, version, language)` 延迟读取与有界缓存，确保列表/详情不读取代码；验证：探针测试精确断言只有 Variant endpoint 触达所选源文件。
- [x] 2.3 实现 `GET /api/templates/themes` 与双语 Theme 响应；验证：API 测试断言认证、固定排序、5 个 Theme、场景计数和无代码/运行态字段。
- [x] 2.4 实现 `GET /api/templates/scenarios` 的 theme、关键词、厂家、类型、协议、语言、成熟度筛选和 1..48 分页；验证：参数化 API 测试覆盖 AND 语义、同语言成熟度匹配、默认 12、空页、非法参数和稳定排序。
- [x] 2.5 实现 Scenario 详情与单语言 Variant endpoint 及稳定 not-found 错误；验证：API 测试断言详情无代码、Variant 只含所选语言、版本一致和错误码稳定。
- [x] 2.6 把模板 router 纳入 Control，沿用业务主体认证且对结构化说明禁用任意 HTML；验证：未认证请求被拒绝，恶意 slug/language 不读取白名单外文件，响应不包含 Secret 或任意 SVG/URL。
- [x] 2.7 验证非 Python 资产进入 wheel 和 Control image；验证：构建/安装 wheel 后通过 `importlib.resources` 读取全部 51 源码，并在镜像内读取代表性三语言 Variant。

## 3. 数据迁移与原子实例化

- [x] 3.1 新增 0032 additive migration、Adapter 两个来源字段和成对 check constraint，并只读扩展 AdapterResponse；验证：0031→0032 与 fresh-head 迁移测试保留旧 Adapter、null/null 默认和不允许半边来源。
- [x] 3.2 增加严格实例化请求 schema 与 `expected_template_version` 校验；验证：名称 trim/空白/长度、extra forbid、未知场景/语言和 409 version conflict 均零写入。
- [x] 3.3 实现 Task 模板专用单事务服务，直接创建 Adapter、Slot 0、空/安全 InputConfig、Revision 1 和 latest pointer；验证：内容与 Variant 字节/结构一致、Worker 为空、manual/stopped、来源正确且允许无 Worker 的专用首 Revision。
- [x] 3.4 实现 Webhook 模板分支，创建全新 disabled Webhook/public id 且 credential 为空；验证：实例化两次 public id 不同，并且不复制 Token、路径或启用状态。
- [x] 3.5 实现 account user/admin owner 与无账户 superadmin system-owned 语义；验证：三类 principal 的 owner/access 响应与额外 ACL 数量精确断言。
- [x] 3.6 实现 201 instantiate endpoint、Location header、现有认证/CSRF 和精确错误映射；验证：API 测试覆盖账户态成功、缺 CSRF、未认证、名称冲突与真实非名称 IntegrityError。
- [x] 3.7 对实例化做负面对象图审计；验证：有演示 Credential 的数据库中，Credential Binding、Dependency、Managed File/Artifact/Lease、Worker、Schedule、ACL、Execution、Admission、Outbox、Attempt 计数均不增加。
- [x] 3.8 注入 Slot/Input/Webhook/Revision 中途失败并验证事务回滚；验证：每个故障点后所有新对象均不存在且名称可重试。
- [x] 3.9 用 Event/Barrier 并发两个同名实例化，不使用 sleep；验证：恰好一个 201、一个 `adapter_name_conflict`，数据库仅一套完整对象图。
- [x] 3.10 回归普通 Create、Clone、Save 与旧 Adapter 响应；验证：现有行为和 Worker 首 Revision 规则未被模板专用例外放宽。

## 4. Recipe 公共合同与验证设施

- [ ] 4.1 实现生成/校验 catalog 内容 hash、行为合同版本和 per-language maturity receipt 的工具；验证：篡改源码、缺 receipt 或跨语言错误升级会使校验失败。
- [ ] 4.2 实现本地 `dlr-cmdb-upsert/v1` fake target，覆盖 begin、资产/关系 batch、finish、幂等重放和同键异 payload 冲突；验证：同一 scan/batch 重放两次仍只有一套对象，冲突不会 finish。
- [ ] 4.3 建立直接加载“已发布 Variant 源码”的三语言 fixture harness，不维护第二份等价实现；验证：测试日志能关联 scenario/version/language/source hash，失败指向真实源文件。
- [x] 4.4 对 51 个 requirements 使用现有 Python/JavaScript/Java parser 并冻结精确版本；验证：所有格式通过 parser，未知/浮动/不兼容依赖被拒绝且没有新增平台依赖。
- [ ] 4.5 建立模板 Secret、真实 Endpoint、账号数据、本机绝对路径和原始异常文本扫描；验证：注入 canary 后门禁失败，匿名 `example.*` 与明确占位符按白名单通过。
- [x] 4.6 建立 Python compile、`node --check`、Java Runtime API compile 门禁并按实际证据更新成熟度；验证：每个标为 syntax-verified 的 Variant 都有对应成功 receipt，未验证云 SDK 不被升级。

## 5. 通用、文件、数据库与传输 Recipe

- [ ] 5.1 实现 REST 单次请求的三语言 Variant，覆盖五种 method、认证、body、timeout、允许状态码、解析、同源重定向和非幂等不重试；验证：本地 echo/redirect/error fixture 执行三份 catalog 源码并核对脱敏与副作用警告。
- [ ] 5.2 实现 REST 分页三语言 Variant，覆盖 page/offset/cursor/next-url、上限、429/5xx 有界退避抖动和四类无推进检测；验证：注入时钟而非 sleep 的 fixture 覆盖四策略、重复 cursor/URL、跨源拒绝和合法 partial JSON。
- [x] 5.3 实现 Webhook JSON 校验标准化三语言 Variant；验证：fixture 覆盖必填、字段改名、嵌套读取、UTC 时间、字段级错误，并断言 adapter_type=webhook 且无 sync。
- [ ] 5.4 实现 CSV→JSON 三语言 Variant；验证：fixture 覆盖 BOM/编码、quoted newline、表头、分隔符、空行、行列/字段/总字节上限，并在 Managed Input 关闭时直接输入成功。
- [ ] 5.5 完成 JavaScript Excel 库维护/安全/许可证评审并冻结版本；验证：评审记录证明同一实现支持 `.xlsx` 与 `.xls`，不满足则在实现前停止并更换方案。
- [ ] 5.6 实现 Excel→JSON 三语言 Variant；验证：真实 `.xlsx`/`.xls` fixtures 覆盖 sheet/range/header/空值、文件/行列上限、公式/宏/外链不执行和依赖安装。
- [x] 5.7 实现 RFC 6901 JSON Pointer 映射清洗三语言 Variant；验证：同一 fixture 对字段选择/改名/default/有限转换/过滤/稳定排序/去重和 `~0`/`~1` 产生完全一致输出。
- [ ] 5.8 实现 PostgreSQL 只读快照三语言 Variant；验证：临时 PostgreSQL 用只读账号覆盖参数化单 SELECT、超时、批读、行上限、多语句/写语句拒绝和失败后连接关闭。
- [ ] 5.9 实现 MySQL 只读快照三语言 Variant；验证：临时 MySQL 用只读账号覆盖与 PostgreSQL 同等合同，并显式断言驱动多语句关闭。
- [ ] 5.10 实现 S3 兼容清单/受限读取三语言 Variant；验证：MinIO fixture 覆盖 continuation、prefix、元数据、Range、小/超限对象、对象数和总字节上限。
- [ ] 5.11 实现 SFTP 清单/受限读取三语言 Variant；验证：本地 SFTP 覆盖正确/错误 host key、服务端 realpath、`..`/symlink escape、过滤、文件数和下载上限。
- [ ] 5.12 实现 ServiceNow `cmdb_ci` 快照三语言 Variant及 preview/sync；验证：本地 Table API 与 fake CMDB 覆盖 encoded query、字段/display value、分页/429/上限、稳定 scan 重放、部分失败不 finish，且不请求 `cmdb_rel_ci`。
- [x] 5.13 汇总通用 11 场景成熟度并只按真实测试 receipt 提升；验证：catalog 校验和 Gallery API 返回的三语言标签与 receipt 一一对应。

## 6. 阿里云 Recipe

- [x] 6.1 从固定来源和官方 SDK 冻结阿里云三场景的三语言包/API/分页与外部键映射，不把矩阵缺口冒充支持；验证：自动比对 metadata 声明、覆盖矩阵和代码入口一致。
- [ ] 6.2 实现 `alicloud-compute-container-topology` 三语言 Variant，覆盖 ECS、云盘、网卡、镜像、伸缩组、ACK 的只读 preview/sync 与有证据关系；验证：脱敏 SDK fixture 覆盖分页、稳定 key、关系、区域部分失败及同 scan 幂等重放。
- [ ] 6.3 实现 `alicloud-network-ingress-topology` 三语言 Variant，覆盖 VPC、VSwitch、EIP、NAT、路由、ACL、VPN、SLB、DNS、证书；验证：fixture 覆盖网络/入口关系、分页、权限缺口、partial 不 finish 和 bounded summary。
- [ ] 6.4 实现 `alicloud-database-middleware-inventory` 三语言 Variant，覆盖矩阵确认的 RDS、Redis、MongoDB、Elasticsearch、OSS、NAS、RAM、KMS、ActionTrail、SLS、安全中心；验证：多产品 fixture 覆盖独立失败清单、确定性排序/去重和无猜测关系。
- [ ] 6.5 验证阿里云三场景的 preview 只读、sync 稳定 `scan_id/source_scope`、目标批次幂等和输出上限；验证：直接运行 catalog 源码，重放同一执行输入后目标对象不重复且任何批次失败均无 finish。
- [ ] 6.6 解析/安装/编译可承担的精确 SDK 依赖并更新九个 Variant 成熟度；验证：仅有对应语言 receipt 的条目升级，未实际联调不得标 live-verified。

## 7. 腾讯云 Recipe

- [ ] 7.1 从固定来源和官方 SDK 冻结腾讯云三场景的三语言包/API/分页与外部键映射，记录 Steampipe LICENSE/SPDX 差异；验证：metadata、覆盖矩阵、NOTICE 和代码入口一致。
- [ ] 7.2 实现 `tencentcloud-compute-container-topology` 三语言 Variant，覆盖 CVM、CBS、镜像、专用宿主机、伸缩组、TKE；验证：脱敏 SDK fixture 覆盖分页、稳定 key、关系、区域部分失败及同 scan 幂等重放。
- [ ] 7.3 实现 `tencentcloud-network-ingress-topology` 三语言 Variant，覆盖 VPC、Subnet、ENI、EIP、NAT、路由、ACL、CCN、VPN、CLB、监听器、目标组；验证：fixture 覆盖 CLB 链路、网络关系、缺权、partial 不 finish 和 bounded summary。
- [ ] 7.4 实现 `tencentcloud-database-middleware-inventory` 三语言 Variant，覆盖矩阵确认的 CDB、PostgreSQL、Redis、MongoDB、SQL Server、MariaDB、TDSQL-C、Elasticsearch、CKafka、COS、CFS、CAM、CLS、WAF、证书；验证：多产品 fixture 覆盖分页、失败摘要、稳定去重及无猜测关系。
- [ ] 7.5 验证腾讯云三场景的 preview 只读、sync 稳定 `scan_id/source_scope`、目标批次幂等和输出上限；验证：直接运行 catalog 源码，重放同一执行输入后目标对象不重复且任何批次失败均无 finish。
- [ ] 7.6 解析/安装/编译可承担的精确 SDK 依赖并更新九个 Variant 成熟度；验证：九个标签逐语言匹配 receipt，未实际联调不得标 live-verified。

## 8. Web 路由、导航与数据层

- [x] 8.1 在写 Ant Design 组件前用固定命令查询 5.29.3 的 Tabs、Select、Modal、Pagination、Skeleton、Empty、Tag API/demo/token/semantic 信息并保留实施笔记；验证：使用 `npx --yes @ant-design/cli@6.6.1 --version 5.29.3 ... --format json`，不升级依赖。
- [x] 8.2 增加模板 API/types、严格错误处理和按 `(slug, version, language)` Variant cache；验证：Vitest 覆盖 query 编码、无代码列表、单语言请求、缓存和 instantiate 专用 endpoint（绝不调用 clone）。
- [x] 8.3 提取轻量 History route 解析并支持 `/adapters`、`/templates`、`/templates/:slug` 与既有 settings；验证：单元测试及直接刷新/popstate 测试覆盖合法/非法路径且无新 router 依赖。
- [x] 8.4 扩展 ApplicationShell 一级语义导航与选中态，保持状态/账号区域；验证：双语渲染、href、SPA 点击、`aria-current`、忙碌禁用和 settings 回归测试。
- [x] 8.5 保持 Adapter surface hidden 常驻并补全代码 dirty + STAGED beforeunload/离开确认；验证：Gallery 往返保留编辑器/运行设置/文件草稿，实例化取消时无 POST，返回后 Monaco 正确 layout。
- [x] 8.6 增加 zh-CN/en `template` namespace 和双语动态字段选择；验证：现有 i18n key、插值和非空一致性测试覆盖新 namespace。

## 9. Web 模板广场与复制交接

- [x] 9.1 按批准概念实现 Gallery 标题、宽搜索、5 Theme、五维筛选、独立页码、三/二/一列卡片、loading/empty/error；验证：组件测试覆盖去抖、Abort/generation 旧响应防线、筛选重置页码和各 Theme 页码恢复。
- [x] 9.2 实现 17-key 原创 Logo tile allowlist，以现有 Ant 图标和受控渐变表达厂家色/场景类别；验证：17 key 快照/DOM 测试、本地离线加载、未知 fallback、`aria-hidden` 和零远程图片请求。
- [x] 9.3 实现场景详情、默认 Python、三语言切换、只读 Monaco、合同/来源/许可证/成熟度展示；验证：只请求所选 Variant，切换同步更新全部语言资产，返回已加载语言不重复请求。
- [x] 9.4 实现成熟度最低级卡片汇总与逐语言详情文字标签；验证：混合成熟度 fixture 显示真实最低级，`reference-generated` 明示实验/未验证且不只靠颜色。
- [x] 9.5 实现复制 Modal 的名称校验、当前语言/版本、单飞提交和 409 保留输入；验证：必填/trim/长度/冲突/一般错误/重复点击组件测试全部通过。
- [x] 9.6 实现成功后的 `refreshAdapters → load returned adapter/revision → /adapters → Edit focus`；验证：组件集成测试看到新名称、语言、模板代码、停止状态，Revision 后加载失败时仍显示已创建对象和真实错误。
- [x] 9.7 在 Managed Input 关闭时保持 CSV/Excel 浏览和复制并显示事实性提示；验证：能力关闭的组件/浏览器用例不禁用详情或 instantiate。
- [ ] 9.8 完成响应式、键盘和焦点行为；验证：axe/可访问性测试以及 Tab/Enter/Space/方向键完成复制，Modal 初始/回归焦点正确，560px 无横向溢出。

## 10. 文档、整体验证与 Draft PR

- [x] 10.1 更新中英文产品、架构和用户文档，说明 5/17/51、复制后独立、Worker/Credential/依赖配置、Managed Input 边界、preview/sync、稳定 scan id、成熟度和非 SSRF 声明；验证：文档链接、双语关键事实和命令示例检查通过。
- [x] 10.2 运行 Backend 定向与全量门禁，包括 pytest、ruff、format-check、mypy、catalog/wheel、migration、事务并发和 Recipe receipt；验证：保存真实命令/结果并区分失败来源，不把静态检查冒充 fixture/live。
- [x] 10.3 运行 Web 定向与全量门禁，包括 Vitest、typecheck、lint、build、i18n 和变更文件 Ant Design lint；验证：全部实际命令结果可追溯且版本仍为 React 19/AntD 5.29.3/ProComponents 2.8.10。
- [ ] 10.4 在本地真实 Control/Web 上执行浏览器流程：直接路由、搜索筛选分页、语言切换、名称冲突、复制后自动编辑、dirty 取消、Managed Input 关闭和零 console/page/未知请求错误；验证：Playwright 业务断言通过。
- [x] 10.5 对 1680、1280、900、560 视口做视觉/溢出/键盘检查，用 `view_image` 同时检查批准概念图与最新实现截图并维护 mismatch ledger 直至无阻塞差异；验证：最终截图不提交仓库，逐项记录匹配/有意差异及理由。
- [x] 10.6 运行许可证、NOTICE、公开文件 Secret/Endpoint/绝对路径扫描，并确认所有 Logo 为本地原创组合图；验证：扫描结果无真实敏感数据、无不兼容代码复制、无远程 Logo URL。
- [x] 10.7 运行与改动风险相称的 Compose/API smoke，实例化一项 Task 与 Webhook 并检查数据库对象图；验证：两者 Revision 1 可读、默认停止、无 Worker/Secret/运行绑定，回归普通 Adapter 流程。
- [x] 10.8 逐项复核 OpenSpec checkbox、`openspec validate --strict`、git diff 和工作树；验证：每个完成项有证据、无调试/生成截图/无关改动，未验证项保持未勾选且风险明确。
- [x] 10.9 提交 `codex/issue132-template-gallery`、推送远端并创建关联 #132 的 Draft PR，正文区分本地验证、Hosted CI、Review、合并和人工验收；验证：远端 head SHA 与本地一致、PR diff/文件树/CI 可见，保持 Draft、不合并也不关闭 Issue。
