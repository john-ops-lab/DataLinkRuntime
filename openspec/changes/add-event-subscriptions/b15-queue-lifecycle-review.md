# B15：队列生命周期错误与恢复审查

## 问题与改动

旧队列运行器将 `receive_event` 的大多数 HTTP 异常统一记为未接管坏消息。平台关闭、激活缺失或配置变化因此可能留下 `blocked_unaccepted`，功能恢复后又被该屏障拦住；RabbitMQ/RocketMQ 没有 Kafka 的跳过入口，原先只能通过外部处置和新 epoch 恢复。

本批按稳定错误含义区分退避、来源配置阻塞、内容故障和容量，不用 HTTP 状态码猜测消息损坏。只有明确内容/身份错误及既有容量摘要进入接管前诊断。T1 后 ACK 失败不生成未接管故障；诊断事务遇到失效 owner 或数据库错误，也不把已完成的客户端关闭误报为未确认。实际关闭失败仍保留许可和原有关闭屏障。

旧误记只允许当前 epoch 的 `source_not_available`、`subscription_activation_missing`、`subscription_config_conflict` 自动恢复。当前激活/配置有效、功能与接收意愿开启、旧 owner 及 closing generation 均已释放后，按既有锁顺序计入元数据额度，原子写入有界系统审计并清除误记。请求冲突、预算不足、真实内容问题或关闭未确认均不放行。不改变 epoch、配置、激活、来源位置和接管事实，不引入跳过、reset 或开关热更新。

## 针对性验证

- 旧代码首败：RabbitMQ 在真实 PostgreSQL 中复现 `source_not_available` 摘要；RocketMQ 使用精确旧版公共运行器和同一真实数据库回归复现相同问题。夹具准备错误与行为首败分开保存，没有把数据库初始化失败当作产品复现。
- `test_issue219_queue_lifecycle.py`、`test_issue219_queue_runners.py`、`test_issue219_runtime.py` 合计 **68 passed / 5 skipped**。受控 native transport 只用于确定性时序；公开两种运行器、T1、租约、Checkpoint、Receipt、Delivery 和配额都使用实际产品代码及 PostgreSQL。
- 覆盖 poll→T1 之间关闭开关、关闭未完成就恢复、激活缺失/配置升版、真正身份冲突、诊断时 owner 失效、T1 后 ACK 失败、容量摘要与成功接管后的清除、旧误记审计幂等，以及真实故障/关闭/无效配置/其他来源故障/预算/审计 ID 冲突的拒绝边界。
- 5 项 skip 属于原文件的可选真实 Broker/故障注入，不计为通过；实际开发环境双来源与配置重启验证另走动态交付门禁。
- 受影响源文件 mypy、Ruff、格式检查，`git diff --check` 和 OpenSpec strict validate 通过。未运行额外本地全量后端或 Web 矩阵；无依赖、迁移或 Web 产品代码改动。

## 审查与交付边界

本批为实施者自审，检查锁顺序、提交前后 ACK、首故障保留、关闭确认、配额回滚、审计冲突/重复、非队列来源兼容和敏感信息边界；未新增阻塞发现，不冒称独立审查。

现有 #221 仍须等待最终完整 HEAD 的四项必要 CI；原私有开发编排沿用固定卷、凭据和旧业务库，实际部署逐文件核对源码及产物。部署后同时启用现有两种测试来源，按启动配置重启 Control 关闭功能，在关闭期间分别投递消息，确认无新接管/执行/目标写入或坏消息屏障；恢复功能后在同一 epoch/配置/激活中经真实 Worker 各写入独立目标一次。随后停用来源并核对监听/缓冲/待处理/执行均归零，复核实际浏览器记录。运行证据、精确 SHA、CI 和部署结果在 PR 当前正文及私有回执记录，本文件不预先宣称动态门禁通过。

保留原 RocketMQ 5.x NORMAL 和 RabbitMQ 专用队列范围。真实 ARM64 RocketMQ ACL Broker、真实 Proxy TLS+ACL 组合等原未验边界不因本批变成已验；不自动合并或发布。
