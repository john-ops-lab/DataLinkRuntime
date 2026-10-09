# Issue #168 本轮开发与证据索引

固定开发基线：`0a71d8280fd4ba8ebf65ed0057ef0121a595b4f4`。本轮保留 #168 的子单结构，排除 #129。每单按完整完成条件独立判断；以下开发回归不自动关闭 Issue，也不代替真实 Linux Worker、浏览器和业务验收。原首次失败与每次恢复分别留存。

| Issue | 基线与最小修复 | 当前证据 | 尚未完成 |
| --- | --- | --- | --- |
| #169 | 真实 uv 安装成功后 `.lock=0666` 导致缓存发布拒绝；把现有锁元数据权限修正扩展到普通源 | 真实 uv/本地 wheel 冷安装、ready 复用、独立 import 与其他可写内容拒绝；受影响测试 65 项通过 | #193 的完整变体与 Linux Worker/UI |
| #178 | 声明 SDK `software.amazon.awssdk:s3:2.32.27` 的真实 javac 复现不存在的 `S3Client.Builder`；使用实际 builder 返回类型并同步目录哈希 | 原样模板在声明 SDK 下冷编译、实际 S3 服务两对象读取、独立字节及 SHA-256 核对；相关模板测试 3 项通过 | Linux Worker/UI 与 #193 完整链 |
| #174 | 破损 JSON/结构错误中断恢复和管理快照；仅隔离可识别的局部坏记录 | limit=1、连续坏记录、重启游标、健康删除推进、未知 guard 保留和真实 I/O 错误不吞；33 项通过 | 真实 Worker 的损坏记录与管理快照 |
| #175 | 通过公开 Claim/Start/guard acquire 建立 running replacement owner + queued 前态；真实 Broker 的 rejected DLQ 消息在 guard 检查抛 409，后续消息未处理 | 只隔离 `cache_reclamation_in_progress`，持久化人工复核 Incident 后 ACK；真实 Broker 两消息继续处理，与 #177 共 28 项通过 | 集成运行、人工解除与恢复验收 |
| #176 | 同一支持前态、真实 RabbitMQ 4.3.5、默认 delivery_limit=5：12 秒内 7 个连接 epoch，两条消息投递计数 0→5，DLQ=2 | 将校验后的同代消息责任交回现有 Outbox，commit 后 ACK_NOOP；Relay 跳过仍被 guard 保护的延后键。修复后 12 秒内 epoch=1、各投递一次、DLQ=0，解除后原行可领取。PAUSE/DEFER 不重置故障退避 | Linux 业务输出、重启、取消与并发窗口 |
| #177 | 缺字段/非法类型/破损 JSON × 四类 x-death，12 项基线失败 | 非法 dispatch 固定 `invalid`，现有诊断字段只保留 allowlist Broker 原因；合法分类不变，27 项通过 | 完整 API/UI 诊断验收 |

真实 Broker 检查使用生产 `V3Consumer` 和真实 PostgreSQL/Control 服务，但资源 envelope 是明确测试替身，未调用 Linux 执行器，不作为业务运行成功证据。S3 宿主探针保留原模板；私有测试 JVM 为 loopback 服务显式设置代理 bypass。本机入口、部署参数、凭据及原始运行日志保存在私有证据中。

受影响缓存/消息回归首轮为 91 PASS / 1 FAIL，唯一失败是原测试仍期待旧 PAUSE 决策；该断言按已设计的持久 Outbox 责任转交语义更新，独立重跑通过。新增故障退避检查 5 项通过。以上计数属于各自检查批次，不能相加当作一次完整测试结果。

#179 的共享规则为 Unicode 码点、区分大小写、稳定排序；null/boolean 字符串为 `null`、`true`、`false`。#195 提供本页“停止等待”，隔离迟到响应并允许再次发送，上游仍受原 deadline/工具预算约束。其余子单及 #191–#194 验收继续进行，BLOCKED 保留原分类。
