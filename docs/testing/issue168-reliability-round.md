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

#179 修复后五语言 × 五个固定完整 JSON oracle 的真实 Linux Worker 执行已全部通过（25/25），每个 workspace cleanup 均完成。共享排序、null/boolean 与转义/补充字符规则分别核对；目录 UI 和其他输入/预算边界继续补证。#173 旧清理 1/2 在既有首次失败和恢复失败后，通过明确人工 retry 完成，首次记录不覆盖。

#184 第三次真实 Linux Worker 跨 TTL 验收完成：合法 TTL=60 秒，周期扫描在长任务运行 88.397 秒时检查到精确被测数字键并记录 cache_entry_in_use；长任务和同版本 warm 任务原样输出、workspace cleanup 完成。任务结束后空闲扫描关联同 round 的 result 为 complete/deleted=1/freed_bytes=35798。第二次 audit 的 cache_lock_busy 与第一 fixture 前态构造失败仍独立保留。

#171/#172/#185–#190/#195 前端已完成最小实现和开发回归，真实 Chrome 验收尚在推进。基线 Chrome 实测包括 129 字符创建错误被遮挡、保留期失焦静默取上限、另一页删除后 AI 草稿仍可选旧凭据、恢复联网刷新成功而全局错误滞留、关闭抽屉焦点落 BODY。#195 当前 HTTP/迟到隔离/再次发送回归已通过；新增用例在旧代码运行时失败，固定版本通过。相关字段/源/凭据/Portable/assistant 组件批次 100 项通过；取消与目录恢复两个独立状态回归通过。初期前端回归两处旧断言（HTTP 401 显示绿色可达、失焦静默钳制输入）已按此次明确修复更新；新的凭据失效提示回归曾暴露 ProForm.Item 未显示帮助，改用精确版本 Ant Design Form.Item 后通过。以上均不计作真实 Provider 或浏览器验收。


#173 实际 Linux Worker 重启后，使用原 idempotency key 重放旧清理 1/2 的人工 retry，均返回原 operation ID、completed、无错误；独立 SQL 核适配器已删除、guard idle、缓存目录为空。原三次自动失败及人工恢复失败继续保留，不作为一次全新首轮成功。

真实 Chrome 已核 #186 创建/重命名 129 字符明确就地报错且输入保留，128 个补充 Unicode 字符可保存并还原测试名；#187 越界/小数原文本在 Enter 与失焦后保留且禁止保存，两个合法边界可用；#188 双页删除后未保存草稿不变、旧选择就地失效、保存禁止，绑定删除保护与保存竞态的配置不变有 API 独立凭据；#189 断网刷新失败后，联网成功清除旧错误且未改变选择/草稿。#187 合法 3600 秒输入对象已通过公开 API 保存并开始真实 TTL 等待，不修改时钟或数据库。

#190 新的真实菜单入口检查暴露两处焦点问题：捕获非聚焦 body，以及 rc-drawer 在 afterOpenChange 之后覆盖焦点恢复。回归分别在前版本先失败；最小过滤可聚焦目标，并将最终恢复延后至下一帧。普通触发器、菜单退场和触发器卸载分别继续验收。#195 真实 Chrome 请求显示停止等待，停止后旧消息保留、立即再次发送并收到新标记的回复；确定性迟到回归另行覆盖旧 finally 不影响新请求。

#180 真实 Provider 首轮：JavaScript 返回候选；XLSX malformed_json；合法 TXT 正确咨询、无候选；Java 文档工具读取成功但最终响应 malformed_json，HTTP 200 是 stopped fallback，未计业务成功；产品帮助 malformed_json。凭据测试前态误删 AI 引用导致的诊断重试失败已与产品首次分列，原配置来源恢复后：XLSX 显式重试返回固定 marker 候选；Java 文档咨询读取当前文档并正确解释三个 Java 字段，无候选；产品帮助显式重试 provider_unreachable。此批诊断直接调用真实 Provider 服务，不替代 HTTP/UI 和人工 Apply/Save/Run 业务验收。未放松 JSON/schema 校验、未启用 Provider 不支持的 JSON mode、未新增自动重试。

#183 首轮实际审计已经取得 initial、工具 followup、finalization 的逐次预算和共享 deadline，固定失败 stage/reason 可区分；真实可选材料裁剪仍待补证。#181 的真实普通问候仍主动提及当前快照和函数名，补充普通帮助不主动审查当前源码的系统指导；不新增关键词路由，知识检索开启时原流程保留。相关四个 prompt 合同回归通过，真实产品帮助仍待复核。

#190 菜单入口的真实 Chrome 仍落 BODY，保留该次失败。进一步确定是设置表单按载入名称/描述重建时再次捕获抽屉内部焦点；把触发器记忆移到稳定外层，保留既有表单重建合同。新的载入中输入聚焦回归在旧实现失败、修复后通过；初版测试仅对不可聚焦 dialog 调用 focus，未形成前态，独立记为构造无效。真实 Chrome 继续验证。
