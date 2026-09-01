# Managed Input Web Specification

## Purpose

定义运行设置、系统设置、上传恢复、示例复制和 Execution 历史的双语交互合同，使用户只操作当前 Adapter 输入并始终看到服务端权威状态。

## Requirements

### Requirement: 输入对象是独立运行设置区
Task 运行设置 SHALL 按运行节点、运行方式、单次超时、Schedule 字段和输入对象组织；manual 与 schedule MUST 复用同一个输入对象区，该区具有独立 dirty、validation、revision 与保存动作，不再嵌入两套 JSON 字段。

#### Scenario: 切换运行方式
- **WHEN** 用户在 manual 与 schedule 间切换运行方式
- **THEN** 同一已保存输入配置保持可见，Schedule 专属 Cron/timezone 按模式显示，未保存输入不会被静默覆盖

#### Scenario: 输入保存失败
- **WHEN** 服务端返回 revision conflict、runtime lock 或校验错误
- **THEN** 页面保留用户草稿并明确提示刷新/修复，不把失败草稿展示为已保存状态

### Requirement: 四类输入卡片遵守能力门禁
页面 SHALL 展示 `none`、`json`、`managed_files`、`remote_files` 四张可聚焦输入卡片；remote_files 始终 disabled 并以双语 Tooltip 标记“开发中”，managed_files 在 feature flag 关闭时 disabled 并标记“尚未启用”。

#### Scenario: Wave A 页面
- **WHEN** Managed Files 后端或 Worker 恢复链路尚未通过且 flag 关闭
- **THEN** 文件卡片可见但不可选择，none/json 正常可用，页面不会发出文件 API 请求

#### Scenario: 直接构造 remote request
- **WHEN** 用户绕过 UI 向后端提交 remote_files
- **THEN** 后端稳定拒绝，页面映射本地化消息且 code 保持不翻译

### Requirement: JSON 编辑器支持完整 JSON 顶层类型
JSON 输入区 SHALL 允许 object、array、scalar 和 `null`，保存前进行语法校验；权威有效配置只在服务端保存成功后更新。

#### Scenario: 保存 JSON null
- **WHEN** 用户输入合法文本 `null` 并保存
- **THEN** 客户端显式发送 `json_value:null`，成功后显示新的 revision

#### Scenario: 非法 JSON
- **WHEN** 编辑内容语法不合法
- **THEN** 页面阻止保存、定位错误且保留最近一次服务端配置

### Requirement: 文件上传使用独立 multipart 客户端
Web SHALL 使用独立 multipart client 携带当前认证、same-origin Cookie、账户入口 CSRF Header 与上传进度，不得把二进制塞入现有 JSON client。Managed Input capability SHALL 下发服务端有序 `allowed_extensions`；Web 的文件选择提示、客户端预校验和双语支持格式文案 MUST 由该字段派生，后端仍是最终权威。

#### Scenario: 账户入口上传
- **WHEN** 已登录账户用户上传允许文件
- **THEN** 请求携带 Cookie 与 CSRF、列表显示进度，认证失败/CSRF 失败不泄露文件内容

#### Scenario: 刷新恢复
- **WHEN** 上传完成为 STAGED 但用户尚未保存并刷新页面
- **THEN** 页面从 staged list API 恢复为“待保存”，不自动绑定或改变 revision

#### Scenario: staged 列表暂时失败
- **WHEN** capability 已成功但 staged list API 暂时失败
- **THEN** 页面保留已加载 capability、retention 与草稿，单独提示列表失败并只重试列表；不得误报策略失败或过度禁用 managed_files

#### Scenario: capability 未知时保存托管文件
- **WHEN** 当前草稿来源为 managed_files 且 capability 正在加载或加载失败
- **THEN** 页面阻止提交该 managed_files 草稿；none/json 保存不受此故障连带阻断

### Requirement: 文件列表完整表达当前与待保存状态
文件对象区 SHALL 展示最多 8 个文件的文件名、扩展名、大小、上传状态、上传时间、服务端过期时间及删除/替换动作；同名冲突按 NFC 与大小写折叠显示，替换表现为新上传后一次保存。

#### Scenario: 第九个文件
- **WHEN** 当前草稿已含 8 个文件且用户继续添加
- **THEN** 页面在文件选择与恢复 STAGED 合并两处都阻止形成超过 8 个的草稿并提示上限，服务端仍作为最终防线拒绝绕过请求

#### Scenario: 同名冲突
- **WHEN** 两个展示名经 NFC 与大小写折叠后相同
- **THEN** 页面标记冲突并阻止保存，后端同样拒绝

#### Scenario: 离开含 STAGED 的页面
- **WHEN** 用户导航离开且仍有未绑定 STAGED Artifact
- **THEN** SPA 路由切换与浏览器关闭都给出离开提示；若用户确认离开，后端 TTL 仍独立回收

#### Scenario: 保存只选择部分 STAGED
- **WHEN** 页面存在多个 STAGED Artifact 而用户只把其中一部分加入本次保存
- **THEN** 保存成功后未选择的 STAGED 仍显示为待保存并可继续绑定或显式删除，不被错误清空或展示为 READY

### Requirement: 空 managed_files 状态明确不可运行
页面 SHALL 允许保存空 managed_files，并显示“输入尚未就绪/尚未上传文件”；运行一次、立即运行和 Schedule 启用动作 MUST disabled 并显示权威 invalid reason。

#### Scenario: 文件全部到期
- **WHEN** 后端系统生命周期转换使当前集合为空
- **THEN** 页面刷新后显示新的 revision、空态和 `managed_files_empty` 本地化说明，不显示陈旧文件

### Requirement: Retention 由服务端计算并受管理员策略约束
文件区 SHALL 提供系统默认、自定义、手动删除（用户文案“永久保留”）三种选择；页面 MUST 从服务端 capability/settings 获取 `allow_manual_delete` 与 `max_custom_retention_seconds`，展示管理员允许范围与服务端返回的具体过期时间，不得用硬编码或自行推算作为权威值。

#### Scenario: 自定义期限超上限
- **WHEN** 用户输入超过管理员上限的 retention
- **THEN** 页面提示范围，后端拒绝绕过提交且不改变当前配置

#### Scenario: 管理员禁用手动删除模式
- **WHEN** `allow_manual_delete=false`
- **THEN** “永久保留”不可选并解释仍受管理员治理；现有已具体化 Artifact 不被自动重算或删除

### Requirement: 服务端 Runtime Lock 是最终权威
Web SHALL 使用 `runtime_locked` 与 InputConfig 响应作为提示，但所有保存、替换、当前文件删除和运行结果 MUST 以服务端 409/422 为准；disabled 控件通过可聚焦 wrapper 或等价机制提供原因。

#### Scenario: 页面状态过期
- **WHEN** 页面显示可编辑但另一会话刚启用 Schedule
- **THEN** 保存收到 `adapter_runtime_locked` 后页面保持草稿、刷新权威状态并提示先停用 Schedule

#### Scenario: STAGED 删除
- **WHEN** 当前配置锁定但用户删除未绑定 STAGED 文件
- **THEN** 页面允许删除，且成功后不刷新为新的 input revision

#### Scenario: Schedule 启用期间上传
- **WHEN** Schedule enabled 或存在 active Execution 且用户选择合法文件
- **THEN** 页面允许上传成为 STAGED 并保留待保存状态，但保存/替换当前 Binding 继续由服务端 Runtime Lock 拒绝

### Requirement: Schedule Adapter 提供无覆盖的立即运行
schedule 模式 SHALL 显示“立即运行一次”，动作只提交空 Execution body并使用已保存输入；不得提供临时 JSON/文件覆盖控件。

#### Scenario: 立即运行成功
- **WHEN** 当前输入有效、Worker 在线且无 active Execution
- **THEN** 页面创建 manual trigger Execution 并开始现有日志 watcher，Schedule cursor 保持不变

#### Scenario: 输入草稿未保存
- **WHEN** 输入对象存在 dirty 草稿
- **THEN** 页面明确说明运行使用已保存配置或要求先保存，不得把草稿偷偷作为 per-run input 发送

### Requirement: 文件读取示例只复制稳定 Context API
保存有效 managed_files 后，页面 SHALL 按 Adapter 当前语言生成 Python、JavaScript 或 Java 示例并提供显式复制；示例 MUST 只使用 Context 文件 API，不含 Control/Worker 路径，不自动写入 Monaco、不保存 AdapterVersion。

#### Scenario: 复制 Python 示例
- **WHEN** Python Adapter 用户点击复制
- **THEN** 剪贴板获得使用 `context.input_files` 的示例，Working Copy 与 latest version 均不改变

#### Scenario: 切换 Adapter 语言
- **WHEN** 用户查看 JavaScript 或 Java Adapter
- **THEN** 示例使用 `context.inputFiles` 对应语言语法且元数据字段与 Runtime 合同一致

### Requirement: Execution 详情按 source 只读展示快照
历史详情 SHALL 对 none 显示无输入、对 json 显示当次只读 JSON、对 managed_files 显示文件名/类型/大小/SHA-256；不得出现输入文件下载、复用、恢复配置、再次运行或内部标识入口。既有 Execution 业务日志下载不是输入文件下载，MUST 保持原有可用性与权限边界。

#### Scenario: 历史 Artifact 已删除
- **WHEN** 用户查看 Blob 已 GC 的旧 Execution
- **THEN** 页面仍从 snapshot 显示审计摘要，但不提供失效下载链接

### Requirement: Adapter 复制页面说明文件不复制
复制 managed_files Adapter 后页面 SHALL 保持文件类型与 retention、显示空集合和重新上传提示；Schedule 保持 disabled且输入有效前不能启用。

#### Scenario: 复制完成
- **WHEN** 用户打开新副本
- **THEN** 页面不显示源 Adapter 文件，不发出源 Artifact 请求，并提示“复制 Adapter 不会复制原 Adapter 的输入文件，请重新上传”

### Requirement: 双语、可访问与响应式验收可重复
所有新增用户文案 SHALL 在 `zh-CN`/`en` 对应 namespace 中 key、插值占位符一致；卡片、上传、表单、Tooltip、错误、历史和复制动作 MUST 可键盘操作并在 1280、1440、1680、1920 宽度无横向溢出或关键操作遮挡。

#### Scenario: 双语视口矩阵
- **WHEN** 浏览器分别以 zh-CN/en 和 1280/1440/1680/1920 打开运行设置、系统设置与历史详情
- **THEN** 文案不显示 raw key，焦点/禁用原因可达，文件长名称可控截断并可查看完整值，页面无横向溢出

#### Scenario: 用户最终验收
- **WHEN** 自动浏览器 Gate 全部通过并保留隔离应用
- **THEN** 系统仅报告“待人工验收”，由用户独立判定视觉与业务 PASS/FAIL；自动 Gate 不冒充用户接受
