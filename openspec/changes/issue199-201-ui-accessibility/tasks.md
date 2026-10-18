# Tasks
## 1. 导航提醒
- [x] 1.1 顶栏及 native 浏览器离开复用工作区 guard；App 测试检查 STAGED 取消/确认、无草稿和既有适配器切换；文档说明边界。
## 2. 表单可访问性
- [x] 2.1 适配器初始焦点及可见 Tab 首尾循环；组件测试和浏览器键盘回归验证 Escape/取消/关闭、无创建请求。
- [x] 2.2 凭据必填字段错误 ID 与 aria 关联、修正清除；中英文组件测试检查无请求及焦点恢复，保留服务端校验。
## 3. 集成检查
- [x] 3.1 Node24 lint/typecheck/build/完整 Web suite 和 AntD lint；OpenSpec strict 校验。
- [ ] 3.2 主 Agent 真实 Chrome 导航、初始焦点/Tab循环/退出、错误 AX/键盘和独立持久化状态验收，记录精确 SHA。

## Verification evidence
- 明确使用 `PATH=/opt/homebrew/opt/node@24/bin:$PATH`。组件专项 AdapterCatalog/SystemSettingsDrawer：38 passed；App code-only Gallery、取消凭据后无提醒、新 STAGED Gallery 提醒：3 passed。
- Node24 完整 `npm run test` 初轮正常完成（124.87s），693 passed/1 failed：旧输入草稿复制用例需要先明确接受新增 Gallery 提醒。更新测试 setup 后该单例通过，原取消 instantiate/无 POST 断言保留。
- Node24 完整 `npm run test` 复核：53 files / 694 tests passed（111.61s）。之后新增“Gallery 隐藏工作区 native Settings”边界，最终 App 专项 3 passed（含新增边界、STAGED 和原取消 instantiate）。全套数字不包含最后新增的一个用例；最终集成 SHA 的 hosted 全套由主 Agent 完成。
- Node24 `npm run lint`、`npm run typecheck`、`npm run build` 通过；最终 native 小边界之后 lint/typecheck 再次通过。构建保留已有大 chunk 提示。
- Ant Design CLI 6.6.1 / snapshot 5.29.3 Drawer、Input info/demo 查询完成，三个修改组件逐文件 lint 均 0 issues；OpenSpec strict 与 git diff --check 通过。
- 合成 API、隔离 Chromium Playwright 回归：新建适配器中英文 2 passed，验证真实初始焦点、16 次 Tab/Shift+Tab、首尾循环、Escape/关闭/取消恢复且无变更请求；凭据错误中英文 2 passed，验证 dialog 内错误 ID、invalid 和浏览器计算后的 accessible description，键盘修正与取消且无创建请求。未连接部署，无真实秘密。
- `ariaSnapshot()` 不包含字段 description，初轮相关测试断言失败；改用 `toHaveAccessibleDescription` 后通过，未因此修改产品行为。
- 部署 Chrome/AX、原始 STAGED 上传与独立持久化状态核验，以及实际屏幕阅读器交互仍属于主 Agent 3.2 的验收范围，保持未勾选；隔离浏览器回归不冒充业务接受。
