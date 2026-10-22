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
- [x] 2.5 更新双语工具/能力合同并通过 AI/预算/安全/协议回归、Ruff/mypy及独立审查，提交第二批PR。

## 3. 界面生命周期与完整验收

- [x] 3.1 实现中英文准备/转换/读取/覆盖/错误状态及范围释放，验证刷新、重挂载、账号/Adapter切换、取消、迟到结果和TTL恢复。
- [x] 3.2 冻结Retry/Regenerate附件refs/hash/revision并保护composer/已冻结/进行中引用，验证删选新文件不会替换旧轮次原件；通过Web/i18n/类型/构建回归。
- [x] 3.3 用真实Chrome和实际视觉模型完成A1图像、A2数字PDF、A3扫描PDF、A4 XLSX、A5真正XLS、A6超预算末页/range、A7业务Candidate/Diff/Apply/Save/Run，按独立oracle保存原件hash和逐项证据。
- [x] 3.4 完成并发、取消/超时/内存/派生字节边界真实进程与存储核验，以及混合附件和无附件旧路径回归；记录所有缺口且不得把partial算pass。
- [x] 3.5 独立审查第三批变更并通过当前HEAD完整CI，在同一固定本机验收入口部署累计HEAD，保存真实镜像/SHA/schema/挂载/认证/Worker和页面结果；提交第三批PR。历史完成证据为 PR #211 的 v0.4.4 交付记录，不表示本次收尾已验收。

真实模型和资源验证范围见 [验收记录](../../../../docs/evidence/issue202-multimodal-attachments/acceptance.md)。3.5 的历史 CI/SHA/部署回执保存在本机私有交付记录中；本次收尾的 CI 与验收由 6.x 单独记录，公开仓库不保存本机入口或部署参数。

## Workflow follow-up

- [x] 4.1 澄清用户授权的附件提取、引用、复述属于数据读取，保留指令权限、来源覆盖和人工执行边界；完整 inline 文本按用户要求仅返回提取内容，prepared 来源与覆盖要求保持。

A16 提示词兼容性验收固定原87字节附件与原请求，对 MiniMax/Kimi 在旧、新提示词下各重复5次，并分别验证角色伪装、权限/Secret请求及代码/执行注入。冻结判据、保存所有尝试及独立审查；真实结果随当前PR更新，不因静态提示词检查或本项完成而将A16其他子项判为通过。

- 用户最终确认验收后再关闭 Issue #202 或归档 OpenSpec；自动测试、Provider协议成功和本机健康不替代用户业务验收。

## 5. v0.4.4 独立审查的四项修复

用户已明确授权并行修复、针对性复测、更新原 PR 与固定本机验收环境、合并七个 PR 并发布 v0.4.4；排除 Issue #129。此授权不改变原 A16 FAIL 的判据或历史结果。

- [x] 5.1 Reader 的 DocumentConversionError 经固定白名单转为稳定工具错误码；解析超时映射 ai_tool_timeout，未知错误保持脱敏，覆盖 list/page/sheet 与原 HTTPException 兼容行为。
- [x] 5.2 scope 在受理时为关闭/清理预留容量，按 Principal 限制活动 scope 与未过期 tombstone 总量；验证其他用户释放、既有活动请求清理、幂等和 TTL，不提前驱逐关闭记录。
- [x] 5.3 首次发送在冻结和清空 composer 前校验真实服务端引用；失效保留文本和文件，校验期间编辑/新增/账号或 Adapter 切换不提交过时草稿，成功发送与 Retry/Regenerate 保持。已由 PR #211 实现并有 Chrome/组件证据；本次核对 v0.6.0 的 sendMessage 仍保留该路径。
- [x] 5.4 tools_supported=False 的 prepared 请求在凭据读取和 Provider 调用前以明确 422 拒绝；提供中英文更换配置提示，旧 inline 与无附件咨询保持。
- [x] 5.5 对最终集成提交完成交叉审查、针对性回归与完整 CI，更新固定本机验收环境并核对原资产保全，记录 Chrome/API 实测和未重跑范围。沿用 PR #211 的历史八项定向补验及保全记录，不冒称重跑 A16。
- [x] 5.6 依次将 PR #205–#211 合并到 main，通过最终 main 精确 SHA 的完整 CI 后发布 v0.4.4；发布说明保留升级迁移和 A16 已知限制，不关闭 #202 或归档本 change。历史 main `7fb4c8f` / CI `39085287163`；PR #211 已合并，后续 v0.6.0 不覆盖原 FAIL。

## 6. 2026-10-22 剩余问题修复与专项验收

基线 v0.6.0 / `5d71062342827fa2cd8b1723454c32066bd08c76`。本节任务为本次交付；上述历史完成状态与首败分别保留。用户要求修复、补验、更新 Issue/OpenSpec 并提交 PR，先不要合并。

- [x] 6.1 将仍可达的 inline 解析改为有界短生命周期隔离进程；保持 API/类型/字节/解析期限/稳定错误，验证真实超时终止、并发许可回收、失败后恢复与不泄露内容。
- [x] 6.2 为 XLSX/XLS 范围添加程序计算的精确统计、计数定义、合并范围作用域及公式/缓存来源；验证 0/false/空格/公式/局部合并，缺失原缓存不被派生重算覆盖。
- [x] 6.3 保持工具字符和总预算不变，裁剪完整 cell 明细并保留原件身份、精确范围统计、合并作用域、覆盖和遗漏；更新提示规则与新 SDK 路径的相关回归。
- [x] 6.4 固定独立 A4 格式组合原件、图像及真值，补日期/货币/百分比/合并/缓存/颜色图表/嵌入图片；当前 MiniMax/Kimi 实测分别核对原件值、显示含义、来源与未读范围。
- [x] 6.5 固定原 A16 材料/问题，验证精确 inline 提取、完整行／普通字段兼容、prepared 来源与角色/Secret/执行注入；按首败增加有界字面字段提示并保留原文和不可信边界。记录全部首次结果、弃用候选与复验，相关模型范围不得泛化为所有 Provider。
- [x] 6.6 完成相称 Backend/工具/真实进程回归、精确 HEAD 四项 CI，并按原私有测试验收编排更新固定环境；真实 Chrome 验 A4/冻结读取与人工业务链，保留卷/Token/Master Key、原资产与原失败，不重跑无关矩阵。
- [x] 6.7 同步 Issue、OpenSpec、部署与验收文档的最终 SHA/范围/结果和历史证据；提交并附加开放 PR 供维护者评审，不合并、不关闭 Issue、不归档本 change；清理本次无用临时材料。


6.x 最终专项证据见 [2026-10-22 收尾报告](../../../../docs/evidence/issue202-multimodal-attachments/closeout-20261022.md)：产品提交 `282b49e` 的四项 CI 通过并部署至同一固定环境；真实 HTTP 为 26 项／28 次尝试，两次明确上游 529 原样保留并各补验通过。两家原 A16 各 5/5、整行／普通字段、原始 17 格与新增 A4 九类事实、prepared 来源及注入边界均已核对。Chrome 的冻结引用和 Diff／Apply／Save／Run、沙箱回执及原资产保全另按其实际 SHA 分层记录。最终文档提交不改变运行源码，其精确 HEAD CI／部署回执由 PR 与 Issue 最终更新给出；本 change 未归档，Issue 不关闭，等待维护者评审和用户最终确认。

## 7. 2026-10-22 维护者确认与归档

维护者已完成独立评审并确认 approve；[PR #218](https://github.com/john-ops-lab/DataLinkRuntime/pull/218) 合并为 `fe5b03f2e8bdea0c91803943719ca23c9e8ddce8`，其 [main CI 39764043921](https://github.com/john-ops-lab/DataLinkRuntime/actions/runs/39764043921) 首轮四项通过，Backend 3044 passed / 17 skipped。维护者随后明确要求关闭 #202、归档本 change 并发布 v0.6.1，取代上述实施与交付阶段的暂不合并／关闭／归档限制。

29 个任务均已完成；三个 capability 的 22 条要求、29 个场景完整同步到 `openspec/specs/`，逐项核对和 strict 校验通过。本 change 归档到 `openspec/changes/archive/2026-10-22-issue202-multimodal-attachments/`；归档与双语发布文档不改变运行源码，最终发布 SHA 和 CI 回执以 PR、Issue 与 GitHub Release 为准。

历史 FAIL、两次上游 529、候选弃用与实际验收 SHA 保持原记录，不因维护者确认改写为首轮通过。解析进程无法确认 reap 时的无内容 warning／指标，以及未测量的 inline 子进程启动延迟，保留为不阻塞后续观察，不扩大本轮实现或验收范围。
