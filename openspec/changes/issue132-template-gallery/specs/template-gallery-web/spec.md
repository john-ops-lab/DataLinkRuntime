## Purpose

为用户提供与现有适配器工作台一致的一级模板浏览体验，使其可按业务场景发现、比较三语言 Recipe，并安全复制后立即继续编辑。

## ADDED Requirements

### Requirement: 适配器与模板广场为一级导航
Web SHALL 在全局顶栏品牌之后提供 `适配器` 与 `模板广场` 两个一级页面链接，并对当前项提供可见选中态和 `aria-current="page"`。`/` 与 `/adapters` 显示现有 Adapter Catalog + Workbench，`/templates` 显示模板列表，`/templates/{scenario_slug}` 显示模板详情；浏览器前进、后退和直接刷新 MUST 恢复对应页面。设置页与现有登录/状态/账号区域 MUST 保持兼容。

#### Scenario: 从适配器进入模板广场
- **WHEN** 用户点击顶栏“模板广场”
- **THEN** URL 变为 `/templates` 且模板广场占用完整主内容区
- **AND** 顶栏“模板广场”显示为当前项

#### Scenario: 直接打开详情链接
- **WHEN** 已认证用户直接访问 `/templates/{valid_slug}`
- **THEN** 页面恢复该 Scenario 详情，而不是回退至适配器或空白页

### Requirement: 模板浏览保留现有工作区草稿
仅在适配器与模板页面之间浏览时，Web SHALL 保留当前 Adapter 的代码、依赖、运行设置和未绑定 STAGED 文件草稿；模板页面显示时 Adapter surface MUST 从布局与可访问性树隐藏。真正切换到实例化产生的新 Adapter 前 MUST 复用统一的 dirty-leave 确认。页面刷新或关闭时，代码 dirty 与 STAGED 文件均 MUST 触发现有 beforeunload 防护。

#### Scenario: 往返模板广场保留草稿
- **WHEN** 用户在 Adapter 编辑器有未保存修改，进入模板广场浏览后返回适配器
- **THEN** 原选中 Adapter 和草稿内容仍然存在

#### Scenario: 忙碌期间禁止页面切换
- **WHEN** 当前工作区正在执行不允许离开的管理操作
- **THEN** 一级导航不得启动冲突页面切换

### Requirement: 列表按批准的主题化卡片布局呈现
模板列表页 SHALL 显示标题、副标题、宽搜索框、厂商/Adapter 类型/协议/语言/成熟度筛选、5 个 Theme 标签页、Scenario 卡片网格和分页。每个业务 Scenario MUST 只显示一张卡片；三语言以可用性和各自成熟度标签展示，不得拆成三张卡。搜索或筛选变化 SHALL 将当前 Theme 重置到第 1 页，各 Theme MUST 独立保留页码。

#### Scenario: 首次进入模板列表
- **WHEN** 用户进入 `/templates`
- **THEN** 页面显示 5 个 Theme、当前 Theme 的第一页和每页最多 12 张卡片
- **AND** 51 个 Variant 不会被计为 51 张卡片

#### Scenario: 切换主题保留独立页码
- **WHEN** 用户在一个 Theme 切到第 2 页、切换其他 Theme 后再返回
- **THEN** 页面恢复原 Theme 的第 2 页

#### Scenario: 列表加载和错误可恢复
- **WHEN** 列表请求加载、无结果或失败
- **THEN** 页面分别显示 skeleton、明确空态或保留筛选条件的重试状态

### Requirement: 过期请求不得覆盖新筛选结果
搜索 SHALL 使用短延迟去抖，列表请求 SHALL 可取消或具有响应 generation 防线。较早请求在较晚搜索、筛选或 Theme 请求之后返回时，MUST NOT 覆盖用户当前看到的结果。

#### Scenario: 快速连续输入关键词
- **WHEN** 旧关键词请求晚于新关键词请求返回
- **THEN** 页面只呈现新关键词对应结果

### Requirement: 每张卡片具有本地原创 Logo tile
每张 Scenario 卡片 SHALL 根据服务端 `logo_key` 使用固定白名单映射，呈现与批准概念图一致的本地“厂家色/主题色 + 场景类别 glyph”Logo tile。Logo 不得从远端加载，不得用用户内容拼接 URL，也不得引入未经授权的第三方商标资产。未知 key SHALL 使用 DLR 通用 Code 图形作为防御性 fallback。图形为装饰时 MUST `aria-hidden`，厂商与场景名称 MUST 始终以文本提供。

#### Scenario: 离线显示场景 Logo
- **WHEN** 页面在无法访问公网的部署中加载全部卡片
- **THEN** 每张卡仍显示一致、清晰的本地 Logo tile
- **AND** 浏览器不发出远程图片请求

#### Scenario: Logo 不是唯一含义来源
- **WHEN** 屏幕阅读器访问 Scenario 卡片
- **THEN** 可从文字标题、厂商和类型理解卡片
- **AND** 装饰图形不会产生重复或无意义朗读

### Requirement: 详情按语言延迟加载完整 Recipe
Scenario 详情 SHALL 展示用途、配置、输入输出合同、安全边界、来源、许可证处理、模板版本和三语言成熟度。默认选择 Python；用户切换 Python、JavaScript、Java 时，页面 SHALL 仅请求尚未缓存的当前语言 Variant，并同步替换只读代码、建议依赖、安装说明、Runtime 建议与成熟度。查看代码和复制操作 MUST 使用当前选中的语言。

#### Scenario: 切换语言同步全部语言资产
- **WHEN** 用户从 Python 切换到 JavaScript
- **THEN** 代码、依赖、安装说明、Runtime 建议、成熟度和来源同时切为 JavaScript Variant
- **AND** 页面不请求 Java Variant

#### Scenario: 会话内复用已加载 Variant
- **WHEN** 用户再次切回已经加载的语言和同一模板版本
- **THEN** 页面使用会话缓存且不重复请求代码

### Requirement: 实验成熟度醒目且不只依赖颜色
卡片和详情 SHALL 以文字显示成熟度。`reference-generated` MUST 显示“实验 / 未验证”等明确语义；卡片汇总状态 MUST 使用三语言中最低成熟度，详情仍分别列出三种语言。任何成熟度表达不得只使用颜色。

#### Scenario: 三语言成熟度不一致
- **WHEN** 一个 Scenario 的 Python 为 fixture-verified、JavaScript 为 syntax-verified、Java 为 reference-generated
- **THEN** 卡片汇总显示最低的实验状态
- **AND** 详情分别显示三个真实标签

### Requirement: 复制 Modal 校验并防止重复提交
详情页 SHALL 提供“复制为 Adapter”操作，Modal MUST 明示当前语言和模板版本，要求用户填写名称并使用现有名称规则进行即时校验。提交期间 MUST 禁止重复提交；409 名称冲突 MUST 保留用户输入并在名称字段附近显示；其他失败 MUST 保留 Modal 与当前 Variant 以便重试。

#### Scenario: 成功复制后自动交接
- **WHEN** 用户输入有效名称并成功实例化
- **THEN** Modal 关闭，页面自动进入新 Adapter 编辑工作区
- **AND** 新 Adapter、选中语言和模板代码可见

#### Scenario: 同名冲突
- **WHEN** 服务端返回 `adapter_name_conflict`
- **THEN** Modal 保持打开、名称保持不变且字段显示可操作错误

#### Scenario: 连续点击提交
- **WHEN** 第一次实例化请求尚未完成时用户再次点击确认
- **THEN** 客户端只发送一次 POST

### Requirement: Managed Input Store 不成为 Gallery 功能门槛
Web MUST 在 `DLR_MANAGED_FILES_ENABLED=false` 时仍允许搜索、查看和复制全部模板。CSV 与 Excel 详情 SHALL 解释用户可在复制后根据部署能力配置输入，但不得禁用复制按钮或伪造文件绑定。

#### Scenario: 关闭 Managed Input Store 浏览文件模板
- **WHEN** 能力响应表明 Managed Input Store 关闭
- **THEN** CSV 与 Excel 模板仍可查看代码并实例化
- **AND** 页面只显示事实性的后续配置提示

### Requirement: 响应式、键盘和双语可用
模板列表在内容宽度至少 1200px 时 SHALL 使用三列卡片，760–1199px 使用两列，低于 760px 使用单列；详情在至少 1100px 时 SHALL 使用说明/代码双列，窄屏上下堆叠。页面不得产生横向溢出。导航、Theme 标签、筛选、卡片操作、语言切换和 Modal MUST 可使用键盘；Modal MUST 具有可见标题、合理初始焦点和关闭后焦点回归。全部 UI 文案及动态 Theme/Scenario 内容 MUST 提供 zh-CN 与 en，且两个 locale 的 key 和插值参数一致。

#### Scenario: 560px 窄屏浏览
- **WHEN** 用户在 560px 视口打开列表和详情
- **THEN** 卡片单列、详情上下堆叠且页面无横向滚动

#### Scenario: 键盘完成复制流程
- **WHEN** 用户仅使用 Tab、Enter、Space 和方向键
- **THEN** 可进入详情、切换语言、打开 Modal、输入名称并提交
- **AND** 焦点状态始终可见且关闭后返回触发控件

#### Scenario: 中英文切换保持结构
- **WHEN** 用户切换 zh-CN 与 en
- **THEN** 所有模板页面文案和动态目录字段切换语言
- **AND** 长文本不遮挡卡片操作或破坏布局
