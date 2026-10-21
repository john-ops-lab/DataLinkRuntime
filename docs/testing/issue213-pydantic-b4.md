# Issue #213：B4 验收计划与材料冻结

核心对照冻结于 `936fddd16277054539419d2ed33e89e7eae5511e`：五项精确 HEAD CI 全部通过（后端 2979 passed / 17 skipped，SDK 领域 parity 658 passed），六个组件构建及本机部署门禁通过。核心样本已开始运行，业务结果待核对，当前不代表 B4 验收通过。

构建首次受 Colima DNS 故障阻断，分别保留基础镜像解析及 npm 下载的原失败。主机下载同一官方 PostgreSQL 基础镜像后校验 manifest/config/layer/diff-id 并载入；仅为本次构建指定包仓库地址，没有修改虚拟机全局网络、仓库 Dockerfile 或原部署门禁。数据卷/凭据/令牌保留，没有数据库恢复；迁移版本仍为 `0046_issue166_retire_ai_history`。

切默认前另补的私有 continuation/预估载荷修正见 [预算记录](issue213-pydantic-b4-budget.md)。真实对照继续使用冻结代码；修正提交独立等待 CI，并在切默认前补受影响验证。

维护者于 2026-10-21 排除本轮 Anthropic/Gemini 在线验收，保留协议功能和合成合同；实际服务验证只覆盖 MiniMax-M3 与 Kimi Coding k3-256k。

核心对照为 2 模型 × 4 场景 × 2 运行器 × 5 次，共 80 个 Assist 样本：咨询、代码修改、原 Excel 的 17 格问题、坏附件后更换正常文件。补充样本最多 24 个，总 Assist 不超过 104 次；以审计核对实际上游请求，最多 400 次，单请求仍为原 150 秒。三次连续鉴权/网络失败停止对应实验；越权、泄密或意外保存/运行立即停止。无按次账单数据，不能给出已花费金额。

代码修改以独立输入输出和真实 Worker 核对；17 格由 openpyxl 原值/原始公式逐坐标核对，模型回答与程序真值分别报告。材料为已有合成语料，不上传用户业务文件。首次失败保留，修复后的复测另记。

| 文件 | bytes | SHA-256 |
|---|---:|---|
| office.xlsx | 11454 | `eb88f7728785d093dbd85c60014941eca07ae811bbb6fecfbc1cb90a857993e2` |
| chart-oracle.png | 14146 | `586f6174aa22d23c032101b8ce01eba09b197108b60fd09e3c77163f4ee591e1` |
| chart-oracle.jpg | 36787 | `a1626494fe34a2d454d388ee0cc7732ff300c13752d035f3ce069b80f99fdcd0` |
| digital.pdf | 66894 | `6eb646cb4fea68c7c8c80feaa753c5e79960b54eccfc04fc5b827e290845bf78` |
| scan.pdf | 80855 | `eee8262c685cd82f87eebf5123ccd0807112f7ff8dacf046ee54797b75e0e67d` |
| xls-export/office.xls | 17408 | `348318a6bc13aa6a55214ccef95f79b1d48d84f332fb703a248bc2cc2420c314` |

PNG/JPEG 的季度、颜色、收入分别为第一/二/三季度、蓝/橙/绿、10/20/30；数字 PDF 第 2 页空白，共 3 页，扫描 PDF 共 2 页且第 2 页空白。XLSX 的 `经营汇总!A1:D6` 有 17 个非空格，B6 原公式缓存 60，D6 原公式缓存缺失，派生页面重算 120 不代表原缓存。恢复 TXT 的标记为 RECOVERY-213，阈值为 73.25。

真实 Chrome、五语言候选与 Worker、图片/PDF/带图表 Excel/XLS/TXT、语言/Retry/Regenerate/scope、资源和新→旧→新回退分别记录；不以协议合成或 API 200 替代业务验收。

## 兼容证据登记（待真实验收补齐）

| 兼容项 | 已有确定性依据 | B4 补充 |
|---|---|---|
| C01–C06 | API/DTO、设置/Catalog、自定义三协议、凭据快照与安全错误合同；B3 已修当前账号/ACL 复查 | 当前启用配置、真实页面连接测试；不要求重录凭据 |
| C07–C12 / C32 | 原 Prompt/五语言/当前代码/语言/候选完成断言经 SDK 复用，预览与帮助 UI 源码未改 | Chrome 的聊天→预览/Diff→Apply→Save→Run、五语言真实 Worker、语言/冻结重生成 |
| C13–C14 / C19–C23 | D6 全分支、知识/附件独立证据、严格参数/输出及有界纠正对照 | 真实附件重复/计数问题单独分类，保留首败 |
| C15–C18 / C20 | scoped store、真实 Reader/pin、三 SDK 媒体 bytes/hash/关联及原格式测试 | 图像、数字/扫描 PDF、图表 XLSX、BIFF XLS、TXT 与坏附件恢复 |
| C24–C28 / C30 | 实际 SDK body 预算、总时限、40/41 共享容量、执行线程 Session、取消/迟到和脱敏审计 | 部署资源/网络次数与残留观察；无根因证据不扩展结论 |
| C29 / C31 | 非 AI OpenAPI、无数据库迁移、非法部署开关拒绝、调用后不静默重跑 | 新→旧→新回退、保存数据保全和临时附件的实际影响；完成覆盖后再切默认/删旧 |
