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

#169/#193 实际 Linux Worker 的 dateutil 普通源冷安装和 READY 复用通过，XLS/XLSX 原文件读取均返回 count=2、cpu_total=10、两个完整原样名称；PostgreSQL 原样模板读取专用只读库的两行完整 JSON，SQL 写入和多语句均在连接查询前拒绝，独立查询确认表不变；SFTP 原样模板与独立客户端均通过实际主机指纹确认、两文件读取与字节哈希，错误指纹与越界 path_escape 均拒绝。Python 原来非法/缺包声明后，在同一适配器保存合法声明、全新冷版本并执行 dateutil 通过，工作区清理完成。构造阶段曾显式发送 input:null 覆盖已保存文件、错误期待 200 而真实 Admission 是 202、将可靠业务失败的 dead_letter 误写为 failed、期待错误返回 output 而有效模板通过异常拒绝；上述首轮脚本错误保留，修正前态/核验方式后仅补未完成分支，未改变业务预期或产品校验。

#178 实际 Linux Worker 首轮在当前 Maven 镜像源解析 dependency 插件超时，终态 dependency_timeout，未到业务代码；一次明确使用官方 Maven Central 的受控重试完成原样 SDK 编译、S3 两对象读取和独立哈希校验，随后恢复原默认源。真实 Chrome 手动再次运行同一保存版本，已安装 SDK 命中、输出两对象和 45 字节内容，清理完成；镜像源失败不被后续成功覆盖。


本轮后续真实证据：#169/#193 的 dateutil、XLS、XLSX、PostgreSQL、SFTP 五个原样已保存变体均经 Chrome 手动运行并核完整输出；常规冷安装、ready 复用、修正声明后的全新冷版本与四个保护负例另有 Linux/API 和独立服务 oracle。#193 受控 A/B PyPI 实际验证长用户名及转义凭据：A 冷安装成功，切换 B 后原 READY 无新增源请求，新版本冷安装实际访问 B；错误凭据得到可达 HTTP 401。

#187 公开 API 保存的 3600 秒对象自然到期，首次读取为 artifact_expired，随后 GC 一次删除完成、无错误；输入配置的保留期仍为 3600 秒。没有修改数据库或时钟。#188 真实 Chrome 在保存请求暂停期间由另一客户端合法删除仅草稿引用的测试凭据，继续提交得到 ai_credential_invalid，草稿保留、旧选择失效，实际 AI 设置不变；与绑定删除保护、断网恢复分别核对。

#176 的实际 Linux guard 前态持续超过 50 秒，排队任务始终 attempt_count=0、Worker 在线、无 Incident；另一适配器成功、另一份排队任务取消。解除后 owner 与排队任务各执行一次，原 marker 输出和工作区清理完成。新增另一适配器验证首次误选没有保存版本的 fixture，纠正为已实际成功的 dateutil UI 任务，原 guard 连续存在且证据未重置；重启分支继续补证。

#175 的实际 Broker 通过专用测试队列真实 reject 两个合法 dispatch 到运行中的 Control DLQ。guard 冲突的 Incident 已持久化为 open；其后已完成任务消息的 Incident 正确持久化为 ignored。专用队列不等于 Worker dispatch 队列，按合同 broker_reason=unknown，未伪造 x-death。guard 存在时人工 recover 返回 409，解除后使用同一 idempotency key 得到 dispatch_already_pending、原 generation 和 Outbox 不变，随后原任务正确执行一次。另一 queued 健康消息的批次分支仍由前述真实 Broker 回归单独验证。

#192 真实 Java 上传在只发送 1 MiB、仍保有预留时，与 Python 预留重叠，第二份因合计超限被拒绝。首个脚本读取上传响应漏了 file 包装层，保留该构造错误；没有重做已完成的并发预留，使用同一上传文件接续下载验证。实际 Control HTTP 下载仅收到首个 4096 字节时，删除返回 builtin_package_busy；完整 6 MiB 下载 SHA-256 与冻结原文件相同，下载后删除成功。原文件库、容量、占用和零预留全部恢复。

#180 JavaScript 固定需求真实 Chrome 返回完整候选，经查看 Diff、手工 Apply、保存、配置原三行输入并手工运行，完整输出包含正确 count/cpu_total/ids/names/items 与额外 message=ok，start/end 日志正确，依赖与运行配置不变、一次 Attempt、清理完成。Apply 后独立版本列表不变，运行按钮要求人工保存。原首次 invalid 记录、早先本轮 API 请求和此轮 UI 请求分别保留。原 XLSX、合法 TXT、Java 文档咨询在代理环境纠正后的显式 HTTP 重试分别返回正确 marker 候选、正确咨询、成功文档读取与字段说明；产品帮助虽 HTTP 200，仍暴露内部字段并描述不准确 UI，未计业务通过。

#190 真实浏览器路由返回系统设置仍保留适配器抽屉，新增 App 导航回归先失败。仅将现有离开页面 portal 清理扩展到系统设置路由，回归通过。#181 加入来自当前 UI 的固定帮助事实卡并明确不推测其他菜单或权限，无工具/普通工具/知识开启三个配置前态先失败、修复后 prompt 相关 28 项通过；真实 Provider 继续复核。


重启续验（2026-10-09）：实际崩溃后的同一 Execution 保留首个 `worker_lost` Attempt，按已冻结的两次上限进入等待、排队，再在真实 Worker 恢复后第二个 Attempt 原样输出成功并完成清理；离线 Admission 的同一任务恢复后仅执行一次。Chrome 历史并列显示两个 Attempt 与原 marker，未覆盖第一次失败。此为明确故障注入补充证据；原 corpus 若要求自然 retry_wait/自然离线前态，仍不能直接把其 BLOCKED 改为 PASS。

专用 Worker 的真实 PATH 临时排除 Java/Go 可执行文件，注册能力如实变为 Python/JavaScript/TypeScript；没有篡改 capability 或制造虚假协议版本。系统状态正确显示，原 Java 绑定的真实运行被 `runtime_worker_invalid` 拒绝，随后恢复五语言 PATH。首个 API 探针误用了已删除 Adapter ID 而返回 404，独立保留并用实际 Java fixture ID 补验。

#176 跨真实 Worker 重启补证：guard 存在时，另一适配器成功、指定 queued 取消且 Attempt=0；Worker 重启后继续观察 50 秒，原 queued 仍 Attempt=0、在线、无 Incident。解除精确 guard 后原 queued 同代执行一次，原 owner 因此次真实重启按原重试策略在第二 Attempt 成功；二者原样 marker、清理、不同 fencing 和责任代次分别核对。owner 的重试增代不能被写成 queued 的重复投递或新增执行。

#181 后续事实卡真实回复未调用工具且 candidate=null，但仍附带代码表达式，且绑定步骤省略选择凭据。对照当前 UI 修正固定事实：填写代码中的凭据名、选择凭据及字段、保存绑定；普通产品帮助省略代码表达式及当前绑定键示例，不宣称所有 Adapter 都需凭据。三种工具/知识配置的回归在修改前失败，修改后完整 Prompt Builder 批次 28 项通过；真实行为复核另行记录。

完整集成检查首次 Web 为 673 PASS/2 FAIL；两个失败分别为目录错误改到局部告警后的旧定位器、测试点击尚在 loading 的按钮。修正测试条件后受影响两文件 172 项通过。Backend 正确专用数据库/Broker 环境的完整批次为 2231 PASS/1 FAIL/22 SKIP，失败来自源扫描把 JSX 行内英文空串误判为用户文案；仅将 validator 分行，针对性扫描 4 项通过。重启后首次扫描调用缺少必需 RabbitMQ 环境，按既有专用测试配置纠正后通过。Ruff、Web lint/typecheck 与 `git diff --check` 分别核验；各批次不合并伪造成一次完整全绿。


续验结果与合同映射见 [issue168-acceptance-map.md](issue168-acceptance-map.md)：原 26 个 BLOCKED 全部列入，13 个原单的 209 条 checklist 逐项承接；未把“部分实证”或明确注入故障的补证改为原测试 PASS。真实 ima 源未配置凭据、合法实际无工具模型和真实旧历史元数据不足仍缺条件；缓存旧版本升级兼容、自然 retry_wait/离线以及原双页计划/满容量等完整矩阵保持待证。

Control `39b0f90` 的固定普通产品帮助在真实 Chrome 首轮得到正确的 Worker/凭据菜单步骤，无工具、无候选、当前代码不变；仍附带代码表达式和绑定键示例，因此 #181 的纯产品表达要求未完全满足，不以 Prompt 测试或 HTTP 200 宣称完成。知识开启但真实源未配置时，HTTP 200 透明说明“本轮未执行检索”，candidate=null、工具为空；不计算为真实知识检索成功。

本轮临时两次/60 秒重试策略恢复原配置，Worker PATH 覆盖移除、实际五语言恢复；原执行冻结快照未被改写，测试计划停用。7 个旧 git archive 构建上下文删除，保留当前运行镜像对应源码和所有首次/恢复证据。数据库、材料/缓存卷、既有 Token/Master Key、#129 和原工作区不动；正常测试目标保留以供审查。

#168 与 #169–#195 共 28 个 Issue 已追加分项开发/验收更新；全部原正文及 open/closed 状态指纹前后相同，#129 未触及。未自动关闭、合并或改写原基线统计。

Draft PR #196 首次精确 HEAD `d1cfb16` 的 Web 与 local-preview CI 通过，Compose smoke 在审计 JSONL 校验失败：检查器只接受原有 tool_attempt/guard/request_terminal，未跟进 #180/#183 新增的 provider_budget/provider_result/response_validation。修正只扩展闭合事件 schema，并对新事件逐项核类型、固定分类、预算总和、窗口与共享 deadline；原事件和全部敏感字面量检查保留。以本机当前及轮转审计文件复核 39 budget、39 result、30 terminal、3 response validation、16 tool attempt、3 guard 全部通过。首次 CI 失败原件保留，最终 CI 结果另行关联精确后继 HEAD；本机应用切换继续等待 CI，不把构建成功视为部署完成。

## 独立 Review 后续（2026-10-09）

用户明确暂停知识库功能开发与真实检索矩阵。真实 ima 来源、知识开关等未完成项单独标记暂停，不删除既有功能、不计作通过；继续更新同一 PR #196。

- #188 修正凭据加载失败误判删除：AI 设置与绑定编辑分别保留最后成功快照、初次未知/失败/已确认缺失状态，提供局部重试并保留草稿。后端保存校验与 ACL 不变。7 个新增回归覆盖初次失败、初次成功被较新失败刷新隔离、重试恢复、实际缺失与后端拒绝；受影响双组件 26 项通过。真实 UI 对最终镜像另行核对。
- #181 先前真实 Chrome 回复虽步骤正确且无工具/候选，仍附带代码表达式。进一步把通用 Secret API 指导限定到代码问题，普通 UI 帮助只解释所问的可见步骤；不使用关键词路由、文本过滤或伪造 Provider 回复。真实 Provider 对新版本的验证另行列账，知识开启矩阵暂停。
- #175 明确现有 DLQ 边界：handler 持久化人工 Incident 后 ACK，不重置原派发行。published 行在 guard 解除后不被 Relay 自动领取，人工 recover 受控增代；confirm-before-mark 窗口的 pending 行保留原 Relay 重放责任，人工 recover 复用原代并解决 Incident。两种真实 Broker 前态、guard 期间 recover 409、健康后继先 Claim、实际 Relay 负例、重复 recover/Claim 幂等全部通过。测试替身上报结果只证明协议责任，不替代 Linux 业务输出。
- #176 慢锁后重新读取 PostgreSQL 时钟，保留最小一秒延后；缺行异常修复保留已验证消息 ID，从权威 Execution 重建其余字段。该分支修复已接受责任，不重新以 ingress 容量拒绝后 ACK；显式删行注入回归不能证明生产正常删除 Outbox。首次回归发现原补建生成新消息 ID，修复后同 ID/代、零 Attempt/Admission 保持通过。
- #174 补破损 JSON/结构错误 → failed_items_page → management_snapshot 的完整性 false、健康记录继续可见和未知 guard 保留；Web 即使失败列表为空也显示局部保护记录警示。补焦点触发器与回退均不可用时不覆盖当前有效外部焦点的边界。两 Web 文件 21 项通过，删除/生命周期/Prompt 批次的其余 139 项首次通过；该批次 3 个失败为未结束 owner Slot 的新测试前态和上述真实缺行身份问题，保留后再针对修复复验。
- 消息责任受影响批次 57 PASS / 2 SKIP；额外非知识库矩阵（cache governance/admin/policy、builtin package、cancel、schedule）122 PASS。首次矩阵命令包含两处不存在的测试路径，未执行用例；核实际文件后使用独立测试数据库重跑，通过，未改产品预期。Web 完整测试、lint/typecheck/build 通过；Ruff、Mypy、OpenSpec strict、diff 检查通过。Hosted CI 与部署按精确新 HEAD 分别报告。
- 在应用检查点 `3e2d5b8` 的真实 Chrome 双页中，第二页启用计划后第一页面自动锁定保存、依赖和全部运行设置，保留未保存依赖草稿；停用后恢复编辑。暂停第一页面实际 POST 保存请求，第二页先启用计划，再放行得到后端 409 `adapter_runtime_locked`，页面刷新权威锁且草稿未丢失；独立网络事件核 409，计划已停用。编辑器原生键盘构造未产生代码草稿，未计通过；使用实际依赖编辑草稿完成规定前态，未保存为版本。

这些补证不自动改写原自然离线/自然 retry_wait、真实旧历史、合法无工具模型和不可执行节点等缺少前态的原 BLOCKED。最终 SHA/CI/镜像/真实 Provider 与 UI 验证结果见 PR #196 的最后状态更新。

Review 续验检查点 `3e2d5b8`：显式 KILL 本任务 Worker 后 Execution 135 从 running 进入 retry_wait（worker_lost），在默认三次/初始五秒退避策略下连续取消两次，终态 execution_cancelled、结束时间相同。恢复原 restart policy 并启动 Worker 后等待在线及退避窗口，仍 Attempt=1；SQL 独立核 Admission 已释放、计数/字节归零及 Slot 无活动 Attempt。这是实际 Linux 故障注入补证，原自然 retry_wait BLOCKED 不改写。缓存升级前态使用已保存 Adapter 32 / Version 30 的 python-dateutil 环境；首次漏传必需输入的业务失败保留，正确输入的 Execution 134 成功且清理完成，原缓存 manifest 与内容/时间摘要冻结并暂时 pin，升级复用结果另行核验。

`4855dc9` 的首次 Hosted Web CI 为 683 PASS / 1 FAIL，已有短 Webhook 日志轮询测试超过 15 秒。原测试在整个控制台挂载前将轮询缩短为 20ms；改为挂载后仅主动推进日志计时器，仍断言短调用发现、读取失败保留旧日志、恢复为新日志、离开后停止读取。定点 1 项及完整 App 149 项通过，原失败 CI/log 留存，未放宽超时或修改业务预期。

`15837dd` 的 Hosted Web 为 683 PASS / 1 FAIL，短 Webhook 测试已通过，失败转为已有缓存 cleanup retry 测试：较慢 CI 实际触发 GET operation detail，但该测试夹具误回列表页，导致 result 未定义。三个 retry 夹具分别补正确 operation 详情；cleanup 用例主动推进一秒并断言恰好一次详情查询，再核同一 sample 的分页合并，避免快速本机掩盖错误。修正中局部变量与测试库 cleanup 同名导致首次检查失败，改名后完整 WorkerCachePanel 16 项、ESLint/typecheck 通过；所有首轮记录保留。没有把真实 API 缺字段改为通过，也未改产品容错或接口合同。DLQ published/pending 两类恢复边界同时写入本 change 的 runtime-cache spec，明确人工恢复要求和原 Relay 责任。


`8ccdebe` 的精确 Hosted CI 四项全部通过：Backend 2240 PASS / 17 SKIP，Web 684 PASS，Compose smoke 与 local-preview 通过。本机四个应用镜像随后切换到相同 SHA，保留 PostgreSQL/Broker、挂载、端口和私有配置；真实 Execution 136 在 Attempt 1 成功、原样输出匹配且 cleanup completed。

旧缓存升级实证：保留 `3e2d5b8` 的 Adapter 32 / Version 30 ready 环境与 manifest/全部内容及 mtime 摘要，升级到 `8ccdebe` 后 Execution 137 在同版本、同输入下成功且清理完成，内容和时间摘要全部不变，没有重新下载或安装。第一检查脚本把 Compose image 标签当作可 inspect 的镜像 ID，在执行前失败；修正为容器实际 Image ID 后通过。此证据证明旧 ready 缓存复用，不证明缺新元数据的更早缓存或真实旧历史。测试 pin 在最终镜像补验后恢复原状态。

最终镜像首轮复验又发现两项：凭据列表请求断网后局部重试恢复了选项和草稿，但共享页面还留着 `network_error`；真实普通工具 Provider 返回正确步骤且无候选/工具，却仍附带未请求的 Secret API 表达式。关闭工具的同模型配置首轮通过，不能据此宣称实际无工具模型矩阵通过。原设置已恢复，临时配置已移除，版本列表和代码不变。

修正凭据元数据失败只显示可重试的局部告警，真实绑定读取/保存错误仍保留共享报告；受影响组件 26 项通过。将同一版本化 Adapter 规则中的 request intent 放在 Runtime Contract 示例之后，明确从 USER_REQUEST 判断任务，普通 UI 问题只给可见步骤，不从代码任务规则推断管理员权限；不增加关键词分流、响应过滤或自动重试，知识流程保持。Backend 相关首次为 235 PASS / 3 FAIL，三个代码候选测试断言旧的 administrator 固定文案；按授权用户的真实合同更新后 238 PASS，候选仅代码、配置手工管理等断言保留。Ruff/format/Mypy 与 diff 检查通过，Hosted CI 和实际 Provider/UI 的后继结果单独关联精确 HEAD。
