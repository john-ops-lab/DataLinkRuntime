## 1. AI 表格附件合同

- [x] 1.1 为 XLS/XLSX 增加扩展名、MIME 与签名一致性校验，并用 focused tests 验证合法文件进入解析、伪装文件返回 `ai_attachment_type_unsupported`。
- [x] 1.2 实现 XLSX 受限 ZIP/XML 单元格投影，验证 shared/inline/numeric/boolean/公式缓存值、空表、损坏 XML、zip bomb 边界、截断和超时均使用稳定结果/错误码且不执行外链或宏。
- [x] 1.3 以 `xlrd>=2.0.2,<3` 实现纯内存 XLS 投影并更新锁文件，验证真实 BIFF fixture、损坏/加密/超维工作簿和 resource release，且依赖安装、许可证检查与 frozen backend 环境通过。
- [x] 1.4 扩展 Web 附件类型、accept 提示和双语错误文案；使用 Ant Design 5.29.3 固定 CLI 快照核对涉及组件，并以 Vitest 验证 XLS/XLSX 预检、提交和既有附件类型无回归。

## 2. AI Managed Input 安全上下文

- [x] 2.1 实现当前 revision 的窄元数据投影并按 ordinal 排序；后端测试验证只含公开标签，不读取 ArtifactStore、不创建 Lease、不暴露 Artifact ID/storage key/路径/Token/文件内容。
- [x] 2.2 为 managed_files 注入三语言稳定 Context 文件 API 与“不代表看到内容”提示；测试验证 none/json 不注入、恶意文件名不能覆盖 system 指令、Provider 消息不虚构文件内容。
- [x] 2.3 运行 AI attachment/assist focused pytest、现有 AI backend regression、Ruff/format/mypy，并扫描日志与 Provider fixture，验证附件正文和 Managed Input 内部字段不持久化或泄露。

## 3. 登录浏览器偏好与认证边界

- [x] 3.1 把登录偏好 helper 与部署系统 locale cache 明确分离，容错 localStorage 不可用/非法值；单测验证首次 `zh-CN`、选择持久化、两个缓存互不覆盖。
- [x] 3.2 在管理员和账户未认证登录页接入局部 locale，验证标题、表单、错误、Ant Design locale 与选择器立即同步且刷新保留。
- [x] 3.3 在登录成功/bootstrap/退出/强制改密边界恢复服务端系统 locale；React 测试验证偏好与系统语言不同时认证前后切换、locale 请求失败不阻断登录、强制改密不允许写登录偏好。
- [ ] 3.4 运行登录/账户 focused Vitest、Web 全量 lint/typecheck/test/build，并在 zh-CN/en 桌面视口验证无闪烁、无横向溢出、键盘可达和 console/request 无异常。

## 4. 文档与同 PR Gate

- [x] 4.1 更新中英文产品/架构或运维文档，明确支持的 AI 附件格式、表格解析限制、Managed Input 元数据边界及“登录偏好仅浏览器、认证态跟随系统语言”，并通过文档链接/双语 key 检查。
- [ ] 4.2 对本 change 与 `issue127-unified-input-object` 分别运行 OpenSpec strict，运行 `git diff --check`、依赖/密钥/绝对路径扫描并记录 exact-SHA；证据明确两项同 PR 但人工验收互不替代。
