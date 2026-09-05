## Purpose

为模板广场提供可审计、可分页且不会把代码或运行态对象混入列表响应的只读目录合同，使 Web 和其他已认证客户端能稳定发现首期官方 Recipe。

## ADDED Requirements

### Requirement: 目录身份与首期清单固定
系统 SHALL 将模板目录作为随 DLR 版本发布的只读 Recipe 资产，并 SHALL 以稳定 slug 唯一标识 Theme、Scenario，以 `(scenario_slug, language)` 唯一标识 Variant。首期目录 MUST 恰好包含以下 5 个 Theme、17 个 Scenario 和 51 个 Variant；每个 Scenario MUST 同时有 `python`、`javascript`、`java` 三个 Variant：

- `cloud-cmdb`：`alicloud-compute-container-topology`、`alicloud-network-ingress-topology`、`alicloud-database-middleware-inventory`、`tencentcloud-compute-container-topology`、`tencentcloud-network-ingress-topology`、`tencentcloud-database-middleware-inventory`、`servicenow-cmdb-ci-snapshot`；
- `api-events`：`rest-single-request`、`rest-paginated-collection`、`webhook-json-normalization`；
- `file-data`：`csv-to-json`、`excel-to-json`、`json-mapping-cleaning`；
- `databases`：`postgresql-readonly-snapshot`、`mysql-readonly-snapshot`；
- `storage-transfer`：`s3-compatible-list-read`、`sftp-list-read`。

目录中的 Theme、Scenario 和 Variant MUST 具有版本化、确定性的数据，不得通过启动后远程下载改变；目录缺项、重复、未知枚举或交叉引用失效时，构建或启动校验 MUST 失败，而不是静默省略坏项。

#### Scenario: 完整目录被校验
- **WHEN** 发布资产通过目录校验
- **THEN** 系统得到 5 个唯一 Theme、17 个唯一 Scenario、51 个唯一 Variant
- **AND** 每个 Scenario 恰好关联三种受支持语言

#### Scenario: 无效目录拒绝发布或启动
- **WHEN** 静态目录存在重复 slug、缺少语言、未知 Theme、未知 Logo key 或无效成熟度
- **THEN** 校验 MUST 明确失败并指出稳定资产标识
- **AND** 系统不得以不完整目录继续提供服务

### Requirement: Theme 查询为已认证只读接口
系统 SHALL 提供 `GET /api/templates/themes`，按显式 `sort_order`、再按 slug 升序返回全部可见 Theme。响应 MUST 包含 slug、中英文名称、中英文说明、排序和 Scenario 数量，且不得包含 Variant 代码或任何运行态对象。

#### Scenario: 已认证用户读取主题
- **WHEN** 已认证业务主体请求 Theme 列表
- **THEN** 系统返回固定排序的 5 个 Theme 及各自 Scenario 数量

#### Scenario: 未认证请求被拒绝
- **WHEN** 未认证请求访问模板 Theme 接口
- **THEN** 系统按照现有业务 API 认证合同拒绝请求

### Requirement: Scenario 列表支持确定性搜索筛选分页
系统 SHALL 提供 `GET /api/templates/scenarios`。请求 MUST 指定有效 `theme`，并可指定 `q`、`vendor`、`adapter_type`、`protocol`、`language`、`maturity`、`page` 和 `page_size`。`page` 默认 1，`page_size` 默认 12 且 MUST 限制在 1 至 48；不同维度筛选 MUST 使用 AND 语义。`q` MUST 对中英文标题、摘要、厂商和标签进行不区分大小写的包含搜索。若同时指定 `language` 和 `maturity`，成熟度 MUST 匹配同一语言 Variant；未指定语言时，成熟度匹配任一 Variant 即可。

返回结果 MUST 按 `featured_rank` 升序、`updated_at` 降序、slug 升序稳定排序，并包含 `items`、`page`、`page_size`、`total`。每项 MUST 仅包含列表摘要、Theme、厂商、类型、协议、标签、`logo_key`、模板版本、更新时间，以及三种语言各自的可用性和成熟度；MUST NOT 包含代码、完整依赖文本、安装说明或 Runtime 配置。

#### Scenario: 默认分页返回摘要
- **WHEN** 用户查询一个 Theme 且未指定分页参数
- **THEN** 系统最多返回排序后的前 12 个 Scenario
- **AND** 响应总数按 Scenario 计算而不是按语言 Variant 计算
- **AND** 响应不含任何 Variant 代码

#### Scenario: 组合筛选作用于同一主题
- **WHEN** 用户同时指定关键词、厂商、Adapter 类型、协议、语言和成熟度
- **THEN** 系统仅返回满足全部维度的 Scenario
- **AND** 相同请求在目录未升级时返回相同顺序

#### Scenario: 搜索或筛选无结果
- **WHEN** 有效查询没有匹配 Scenario
- **THEN** 系统返回成功响应、空 `items` 和 `total=0`

#### Scenario: 无效查询被拒绝
- **WHEN** Theme、枚举、页码或 page size 无效
- **THEN** 系统返回可识别的验证错误且不回退到其他 Theme 或默认枚举

### Requirement: Scenario 详情与 Variant 内容分离加载
系统 SHALL 提供 `GET /api/templates/scenarios/{scenario_slug}` 返回场景级用途、详细说明、输入输出摘要、模式、风险、标签、模板版本、更新时间、Logo key、来源摘要以及三个 Variant 的语言和成熟度摘要；该响应 MUST NOT 包含代码。系统 SHALL 提供 `GET /api/templates/scenarios/{scenario_slug}/variants/{language}`，仅返回所选语言的代码、建议依赖、安装说明、非敏感输入骨架、输入输出合同、安全 Runtime 建议、成熟度和完整来源记录。

#### Scenario: 查看场景详情不提前加载代码
- **WHEN** 用户打开一个有效 Scenario 详情
- **THEN** 系统返回场景说明与三语言摘要
- **AND** 响应不得包含任一语言的代码正文

#### Scenario: 只加载所选语言
- **WHEN** 用户请求有效 Scenario 的 `javascript` Variant
- **THEN** 系统只返回该 Scenario 当前版本的 JavaScript 资产
- **AND** 不返回 Python 或 Java 代码

#### Scenario: 未知场景或语言
- **WHEN** slug 不存在或语言不在受支持枚举中
- **THEN** 系统分别返回稳定的 `template_scenario_not_found` 或 `template_variant_not_found` 错误

### Requirement: 模板版本防止预览与复制漂移
每个 Scenario SHALL 具有不可空的模板版本；同一 Scenario 当前的三个 Variant MUST 共享该版本。目录内容、合同或代码发生发布级变化时 MUST 更新模板版本。所有 Scenario 详情和 Variant 响应 MUST 返回该版本，供实例化请求作乐观一致性校验。

#### Scenario: 三语言共享展示版本
- **WHEN** 客户端依次读取一个 Scenario 的三种语言 Variant
- **THEN** 三个响应的模板版本与 Scenario 详情完全一致

### Requirement: 来源与成熟度逐语言真实呈现
每个 Variant SHALL 独立记录成熟度，值 MUST 为 `reference-generated`、`syntax-verified`、`fixture-verified` 或 `live-verified` 之一。每条来源记录 MUST 包含 URL、固定 commit/tag、参考文件或官方 API、许可证分类、使用方式和核对日期。GPL、ELv2、无许可证或其他不兼容来源 MUST 标为仅行为研究，不得声明为代码改编来源。系统不得因另一语言验证通过而提升当前 Variant 成熟度。

#### Scenario: 未执行的验证不被升级
- **WHEN** Python Variant 已通过 fixture，而 Java Variant 只完成语法校验
- **THEN** API 分别返回 `fixture-verified` 和 `syntax-verified`
- **AND** Scenario 摘要不得把 Java 展示成 fixture 已验证

#### Scenario: 不兼容许可证来源可审计
- **WHEN** Variant 参考 GPL、ELv2 或无许可证项目中的行为
- **THEN** 来源元数据标明 `behavior-research-only`
- **AND** 该来源不得被标明为改编或复制

### Requirement: Logo 标识安全且可离线解析
每个 Scenario SHALL 返回一个受控白名单中的稳定 `logo_key`，用于前端映射本地原创的厂家色与场景图形组合。API MUST NOT 返回远程图片 URL、任意 SVG/HTML 或由请求参数拼接的资产路径；未知 key MUST 在目录校验阶段失败。

#### Scenario: 列表只返回受控 Logo key
- **WHEN** 客户端读取任一 Scenario 摘要
- **THEN** `logo_key` 命中发布版本的固定白名单
- **AND** 客户端无需联网下载第三方 Logo

### Requirement: 模板浏览不依赖运行态与 Managed Input Store
目录读取 SHALL 不创建或查询模板专用 Adapter、Revision、Credential、Dependency、Managed File、Worker、Schedule、Webhook 或 Execution。`DLR_MANAGED_FILES_ENABLED=false` 时，所有 Theme、Scenario 和 Variant 读取接口 MUST 保持可用，包括 CSV 与 Excel 模板。

#### Scenario: 默认关闭 Managed Input Store
- **WHEN** 部署关闭 Managed Input Store 且用户读取 CSV 或 Excel Variant
- **THEN** 系统正常返回模板说明与代码
- **AND** 不创建托管文件或文件绑定

### Requirement: 模板响应不泄露敏感或可执行内容
模板 API SHALL 仅返回仓库审核过的公开 Recipe 资产。代码、示例、输入骨架、来源和错误响应 MUST NOT 包含真实 Secret、Token、账号、Endpoint、本机绝对路径、用户数据、模型推理、提示词或原始第三方响应。详情展示 MUST 将说明作为结构化文本处理，不得要求客户端执行 HTML。

#### Scenario: 恶意路径不能读取任意文件
- **WHEN** 请求中的 slug 或 language 包含路径穿越或未知值
- **THEN** 系统返回验证或未找到错误
- **AND** 不读取目录白名单之外的文件

#### Scenario: 目录资产通过敏感信息检查
- **WHEN** 发布流程扫描模板清单、代码、来源和示例
- **THEN** 只允许明确匿名的占位值
- **AND** 发现疑似真实 Secret、Endpoint 或本机路径时发布校验失败
