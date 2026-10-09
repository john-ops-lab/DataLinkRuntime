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

AI 诊断批次：#180 的固定安全分类与 #183 的逐次预算/时限事件加入现有有界轮转 AI audit，不新增 Prompt/响应存储。9 个错误层与 2 个工具调用链基线均缺少诊断；修复后相关小批 22 项通过。扩大检查为 379 PASS / 1 FAIL，唯一失败是新增线级 JSON 测试采用了当前 CPython 能解析的嵌套深度；按本机实际解析器阈值修正测试前态后，6 个线级分类检查通过。首次结果独立保留。真实 Provider 四场景与两个原 BLOCKED 仍待运行。#182 完全相等的历史 envelope 在普通/多模态两种前态均先失败；最小改为对象身份定位当前请求后，同组 11 项通过，完整当前材料与工具配对保留。

#173 的 Linux 首次链路已复现：实际 active Python 任务 stop/delete 返回等待 Worker，取消与工作区清理完成后适配器删除，但环境清理到 3 次仍失败。独立定位为 `cache_rebuild_unknown`：普通无依赖环境没有可重建证明，已删除适配器的清理误走 live-version GC 证明门槛。修复仅区分 Control 授权且无引用的 deleted-Adapter cleanup 与普通 GC；owner、内容/摘要、Pin、最后使用时间、local use/journal、cleanup claim/observed identity/guard 仍检查，普通 GC 仍要求可重建证明。预算耗尽后已完成的扫描等待既有删除恢复，不反复扫描发起新删除。新增无证明清理/重启恢复/活动引用/Pin 回归；相关缓存与替换/策略批次 69 项通过。

#184 的基线缺少模式和按键扫描审计，两个新增回归先失败。审计加入现有 Worker 轮转日志，以 round ID 关联 scan/result，区分 periodic/pressure/manual，记录分页完整性、游标摘要、最多 200 个已验证数字键、固定原因及预算/释放计量。不记录 identity、路径、proof/actor、任意目录名或异常正文。单元链已证明 active use 保留并在释放后安全删除；真实 Worker 跨 TTL 证据仍待核对。新增检查第一次因把业务字节当作物理占用而失败，纠正为独立缓存账目值后，上述 69 项全部通过。各轮首次记录单独留存。

#179 实际 Linux Worker 基线：五语言 × 同一组四个固定输入共 20 次均 `succeeded` 且 workspace cleanup 完成，但 10 次完整 JSON 不符合共享 oracle。除了大小写/null/boolean，Java 在补充字符与 BMP 字符混排时表现为 UTF-16 顺序。新比较器使用 Unicode 码点并保留稳定序号；字符串键直接比较实际内容，覆盖控制/转义字符，null/boolean 拼写统一，原过滤、去重、输入和输出预算不变。三语言宿主直接链的首次结果 6 FAIL/6 PASS，追加转义字符边界也先失败；修复后共享规则与相关模板/资源哈希批次 20 项通过，TypeScript/Go 的真实修复后执行继续单列。

#170 真实 Java/Go 两个非法声明首轮都是 `dependency_preparation_failed` 且日志只有通用准备失败。新增四个中英文 executor 回归先失败，修复后按稳定 `dependency_declaration_invalid` 输出行号和当前语法，禁止回显声明正文，四项通过。未扩大声明语法；不存在版本与合法冷准备另行核对。

#171 使用真实本地 HTTP 认证服务复现：无凭据得到可达 401，带长用户名/转义字符的错误凭据却错误变成无 HTTP 的不可达。probe 仅将 installer URL 的 userinfo 转成 urllib 标准、按 URI 限定且不随跨源重定向发送的 Basic auth；Worker URL 合同保持。HTTP 响应仍表示服务可达，传输错误改为固定 DNS/TLS/connection/timeout 分类，不回显 exception 或地址。首个窄回归修复后通过，跨源重定向与 API/冷安装验收继续。

真实 #184 第二次 fixture 已取得 active 跨 60 秒 TTL、周期按键覆盖、实际业务和同版本 warm 输出；审计只给出 `cache_lock_busy`，未充分区别活动引用。跨线程 durable use 回归明确先失败，正在把只读 preview 的原因细化为已证实的 use/journal 保留；不改变删除授权。第一 fixture 在 active 下提交 protect 命令无法及时取得锁，保留为前态构造失败，已改为先正常执行、再通过公开管理 API 确認可重建，随后启动长任务。

#173 旧失败人工重试进一步暴露 `cache_retry_unknown`：失败发生在 guard/本地记录创建之前，重试却只接受已有 failed 删除记录。补充真实首次恢复失败与窄回归；仅对 `cache_retry_unknown` 且本地记录状态 clear 允许新 Control cleanup claim 重新进入原 guard 扫描，未知/损坏状态继续失败，未改变自动重试上限。

上述 #170/#171 相关 API、真实认证服务/重定向和 executor 检查批次 26 项通过。首个批次 12 PASS/14 ERROR 的原因是从仓库根目录运行 PostgreSQL fixture，找不到相对 `alembic` 路径；按项目 backend 工作目录重跑，通过，不计为产品缺陷。#173 预记录失败恢复与 #184 跨线程原因相关批次 23 项通过；此前按键/策略/删除批次 54 项通过，各批次单独记录。
