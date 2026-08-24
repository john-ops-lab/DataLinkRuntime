# Issue #117 Batch 6 evidence

`DISPATCH_ID=issue117-b6-ai-20260825-r1` 仅覆盖 OpenSpec tasks `6.1`–`6.3`。
基线为 `5bedf1121456d4ee2a19433dd9399c2e9ea48aa2`；Batch 7+ 未实现。

## 实现边界

- AI Assistant 继续复用现有说明容器、附件 API、校验和 i18n。
- 可见紧凑文案把数量、单文件大小、总大小和隐私提醒放在同一说明容器；桌面
  `1280/1440/1680/1920` 保持完整单行，没有 `<br>`、可见块级分段或省略号。
  `<=1180` 使用可读的 `flex-wrap` 降级，完整隐私提醒换行但不被裁切。
- 桌面与窄屏说明字体均为 `0.68rem`；窄屏依靠布局换行，不使用 `0.54rem`/`0.44rem`
  等不可读字号。
- 原有完整 `hint`、隐私说明和附件支持类型保留在说明父节点及子节点的
  `aria-label`/`title` 中，避免可见紧凑文案改变可访问语义。
- 上传、拖拽、类型/数量/大小校验、移除和隐私处理实现未改；未改 API、Provider、
  Secret、附件合同或依赖。

## 验证

- focused render/regression：`assistant-wave-b3.test.tsx` Batch 6 与 `assistant-wave-b.test.tsx` `M5.8-009` 通过；单测聚焦文本、aria 与附件生命周期，不读取 CSS 源码。
- auxiliary Chromium：`auxiliary-matrix/browser-report.json` 的 12 个用例全部通过，覆盖
  `zh-CN`/`en` × `1100`/`1180`/`1280`/`1440`/`1680`/`1920`；`1100/1180` 断言可读换行、
  完整隐私文案与无裁切，桌面四档断言单行、`flex-wrap`、子元素 `white-space`、
  `scrollWidth <= clientWidth`、整页水平/垂直溢出、上传/拖拽/错误/移除、lifecycle 和
  fixture assist request。
- full Web gates：`npm run lint`、`npm run typecheck`、`npm run test`（30 files / 343 tests）、
  `npm run build` 通过。
- strict OpenSpec：`openspec validate issue117-manual-test-fixes --type change --strict` 与
  `openspec validate --all --strict` 通过。
- primary AO Browser：见 `ao-browser/README.md` 与 `ao-browser-report.json`；该记录只保留
  可访问文本、请求 metadata、console/error 计数，不保留原始 response/body。
- AO Browser primary artifact 采用 sanitized snapshot/request/console/error report；
  `ao browser screenshot` 未在本次 AO panel 会话中返回或写出可归档图片，因此没有伪造
  AO screenshot。视觉和 overflow 证据只引用下方辅助 Chromium 的十二张截图。

## 隐私与证据隔离

AO Browser 和 Playwright 均使用匿名 fixture。没有真实凭据、Secret、Provider 原始响应、
本机绝对路径或生产数据进入归档。AO Browser 的附件按钮实际打开动作随后取消 native
chooser，实际文件 picker/drag/drop/invalid/remove 生命周期由同一 fixture 的辅助 Chromium
矩阵完成；两类证据在目录和报告中明确区分。英文紧凑文案保留
`To admin-configured model · No passwords/keys or sensitive credentials.` 的隐私语义；完整
原文仍由 `aria-label`/`title` 提供。
