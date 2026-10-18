# Tasks

## 1. 附件表示、资源与转换

- [x] 1.1 用同一套数字/扫描 PDF、含图表 XLSX、真实 BIFF XLS 验证固定 Docling CPU/LibreOffice/openpyxl/xlrd 组合，记录版本、字体、权重、SHA-256、覆盖、延时和内存；记录单组件不足及实际取消证据。
- [x] 1.2 实现独立临时附件库与 prepare/get/release/scope-release API，验证 Principal/Adapter/scope/请求引用隔离、hash/TTL/refcount/容量、释放和旧inline兼容。
- [x] 1.3 实现受限转换服务、原始页号/outline sheet映射和按页生成真实像素，验证原件不变、空白页、扫描OCR、错误覆盖、子进程实际终止与资源清理。
- [x] 1.4 实现有界 XLSX/XLS 原件metadata/range读取，验证 raw/format/merged/formula/cache/derived来源及宏/外部关系禁止执行。
- [x] 1.5 添加固定镜像/依赖/字体/权重及 CPU Compose配置、双语部署与回滚说明；通过 Ruff/mypy/API测试、Compose渲染及真实隔离资源检查，并提交第一批PR。

## 2. 模型能力、只读工具与预算

- [x] 2.1 实现精确 Provider/model/endpoint/protocol 图片与PDF能力resolver，验证已知/未知/custom声明和不自动切换模型。
- [x] 2.2 注册目录/单页/sheet-range只读工具，验证当前请求白名单、参数/范围限制、原件hash及剩余期限，拒绝路径/URL/跨范围引用。
- [x] 2.3 实现 OpenAI-compatible/Anthropic/Gemini 实际多模态工具后续传输，验证来源关联、真实像素/文件parts与全工具call/result关系。
- [x] 2.4 扩展每次Provider调用token/图片/PDF/wire bytes预算，验证首次、后续、最终化和纠正均经过门禁，覆盖裁剪说明和无界循环禁止。
- [ ] 2.5 更新双语工具/能力合同并通过 AI/预算/安全/协议回归、Ruff/mypy及独立审查，提交第二批PR。

## 3. 界面生命周期与完整验收

- [ ] 3.1 实现中英文准备/转换/读取/覆盖/错误状态及范围释放，验证刷新、重挂载、账号/Adapter切换、取消、迟到结果和TTL恢复。
- [ ] 3.2 冻结Retry/Regenerate附件refs/hash/revision并保护composer/已冻结/进行中引用，验证删选新文件不会替换旧轮次原件；通过Web/i18n/类型/构建回归。
- [ ] 3.3 用真实Chrome和实际视觉模型完成A1图像、A2数字PDF、A3扫描PDF、A4 XLSX、A5真正XLS、A6超预算末页/range、A7业务Candidate/Diff/Apply/Save/Run，按独立oracle保存原件hash和逐项证据。
- [ ] 3.4 完成并发、取消/超时/内存/派生字节边界真实进程与存储核验，以及混合附件和无附件旧路径回归；记录所有缺口且不得把partial算pass。
- [ ] 3.5 独立审查第三批变更并通过当前HEAD完整CI，在同一固定本机验收入口部署累计HEAD，保存真实镜像/SHA/schema/挂载/认证/Worker和页面结果，提交第三批PR且不自动合并。

## Workflow follow-up

- 用户最终确认验收后再关闭 Issue #202 或归档 OpenSpec；自动测试、Provider协议成功和本机健康不替代用户业务验收。
