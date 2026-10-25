# B1：公共责任与管理底座审查

实现提交：`fe04ff1`。本记录对应公共底座，来源注册表为空且功能默认关闭，不能据此宣称 Kafka 或 Kubernetes 已可用。

## 已完成验证

| 范围 | 结果与边界 |
| --- | --- |
| 合并 PostgreSQL 回归 | 71 项通过：交付 30、管理 25、升级 2、兼容 14；覆盖 T1/T2 并发去重、事务中断、Admission、不可变快照、repair、ACL、额度与留存 |
| 独立审查 | 57 个不同的公共核心、管理与迁移用例通过；审查者未修改这些实现，兼容改动由主 Agent 审查 |
| 既有路径兼容专项 | 122 项后端用例通过，含 Adapter、Credential、clone、普通执行、输入、定时、Webhook 和留存；与合并集有重叠，不累加为独立总数 |
| Web | 执行历史 47 项通过，含中英文订阅标签和 `revision=null`；typecheck、ESLint 通过 |
| 后端静态检查 | Ruff、424 个文件格式检查、208 个源文件完整 mypy 通过 |
| 公开提交检查 | staged diff 检查、Gitleaks 及环境信息模式人工复核通过；原始配置和证据保留在私有目录 |

迁移使用真实 PostgreSQL：从 `0046_issue166_retire_ai_history` 升级至 `0047_issue219_subscriptions`，验证已有 Adapter、代码版本、普通 JSON 输入与 revision 保持不变；fresh schema 没有默认订阅、事件或 Adapter。

## 审查关闭的问题

- T1/T2 遵循 Adapter → Subscription → Lease/Checkpoint → Receipt/Batch → 配额 → Admission/Execution/Outbox 的锁序；来源 I/O 不进入事务。
- 首次 fencing 固定实际原生 owner 的关闭代次，多次停用或恢复不覆盖它；晚到 ACK 不能清除新 owner，实际关闭后才回收。
- 隐藏订阅与未知订阅返回相同 404 详情；列表先按权限过滤再分页和计数，校验错误不反射拒绝的 Secret 输入。
- 当前配置 replay 仅排除所选失败事件，仍拒绝其他未完成责任；原快照、旧执行终态和后续 pending 不改写。
- 额度按事务记账，低频后台只读核对；显式停用并确认关闭后可保留 pending 修复计数，修本订阅事实账本后重建全局聚合，避免独立漂移导致错误负差额。
- 无 Receipt 的 skip/reset/额度修复审计也占用去重预算，并有 30 天清理窗口。
- GC 先筛可清记录再每页最多 100 条，过期原文空壳或受保护批次不会阻挡后页；删除订阅后继续执行 7/30 天清理。
- 超过窗口且无引用的旧 activation/config 释放凭据引用；仍有效的当前激活、未完责任和失败屏障继续保护。

## 开发容器与浏览器

开发测试栈已运行实现提交 `fe04ff1`，Control、Worker 和两个 Web 的实际文件哈希全部一致，数据库向前迁移至 `0047`。迁移前备份可读，原始数据库、持久卷、密钥及非应用容器均保留。

本机首次按公共 Dockerfile 构建时，容器 DNS 无法解析 Python 包下载域名。依赖锁文件、Python 版本以及 Web manifest/lock 和 Nginx 配置逐个核对与基线相同后，使用已安装相同依赖的基线镜像覆盖本批源码；Web 运行正式 typecheck/Vite 构建并替换产物。首败和复用依据均保留，未把本次结果写成公共 Dockerfile 在线完整构建通过。

一条明确标为合成来源的事件经 T1 接管后，在服务端开关保持关闭、来源注册表仍为空时，经真实 T2 → RabbitMQ → Worker 成功执行，输出与持久 Envelope 完全相同，公开 `input_snapshot={source_type: json, revision: null}`。同一 Adapter 升级前和排空后各一次普通手动执行均成功，保存输入与 revision 不变，额度核对无差异。这证明公共交接和现有 Worker JSON 协议，不能作为 Kafka 来源或业务目标验收。

Chrome 真实浏览器验证中英文的历史触发标签、详情和只读 JSON 输入，未显示 Task 输入 revision；截图已实际查看，界面无本批显示问题，浏览器 error 日志为空。语言已恢复中文。原始截图包含私有环境信息，保留在私有证据目录。

本批验证和独立审查通过，批准进入 B2。

## 后续门禁

C01–C10、C13–C15 在本批证明的是公共事务、API、迁移和兼容部分。Kafka ACK/rebalance、原生监听停止、真实来源到业务目标读回、持续容量和完整产品浏览器流程分别归属 B2–B5。

当前工作流仅对 main push 或 PR 触发托管 CI。本批保留本地静态和相关测试证据；最终统一 PR 必须通过其精确 HEAD 的配置 CI，不把本地结果写成托管 CI 已通过。PR 不自动合并。
