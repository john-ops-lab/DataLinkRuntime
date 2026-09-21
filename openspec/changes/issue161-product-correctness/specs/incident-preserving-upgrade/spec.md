## Purpose

定义第二组产品修复在保留第一组运行责任及审计的固定环境中进行一次性同 schema 更新的行为边界，并要求精确版本审查、双入口验证、旧资产完整保全和真实探针后才声明部署成功。

## ADDED Requirements

### Requirement: 第二组更新绑定独立批准的精确候选
系统 SHALL 仅对本组受限 `audited-group2-same-schema-v1` / manifest v4 支持同 `0040_issue152_dispositions` 更新，MUST 将具体批准、独立代码审查、完整 Git 差异、迁移图、controller、镜像与精确候选 CI 绑定为一次性准入依据；普通模式、v2 与第一组 v3 规则 MUST 保持。

#### Scenario: 精确对象发生变化
- **WHEN** 安装、规划、选择或最终切换时发现 base/head/tree、任一路径 status/mode/blob、迁移图、controller、镜像或 CI 与独立冻结记录不符
- **THEN** 系统拒绝推进，不从当前 diff 自行生成批准，不自动重绑 manifest 或扩展到后续组

#### Scenario: 成功后再次选择其他候选
- **WHEN** 已消费的 manifest 被用于其他 head 或未完成事务被要求标记成功
- **THEN** 系统拒绝，不复用第一组或本组旧探针与成功证据

### Requirement: 更新全过程保留原责任及全部旧资产
系统 MUST 在一致的只读快照中保护全部原责任、完整审计、业务及账号会话行，并保护旧文件、权限、材料、卷、凭据和显式配置；fresh baseline SHALL 先与独立封存事实核对。备份、停机后真实 idle kernel、同 schema no-op 及启动前后保全 MUST 完整通过，未知漂移 MUST 阻断成功，禁止通过删除责任、降级数据库或重置状态绕过。

#### Scenario: 旧责任或审计改变
- **WHEN** 原 queued、cleanup、terminal、零 Attempt pending 占位、完整审计或旧用户/会话/资产发生未获准变化
- **THEN** 保留失败证据及 attention，不能写 ready/current 或消费 manifest

#### Scenario: 新探针自然完成并收尾
- **WHEN** 本候选完成全新 RabbitMQ→Worker 实际执行与自然 cleanup
- **THEN** 系统依据当次精确对象、窗口与内容验证新增量，全部旧行及旧文件内容仍受保护；未知文件或全局时间戳豁免不能用于通过后置检查

### Requirement: 双入口沿原绑定部署并验证实际身份边界
第二组模式 SHALL 沿既有显式私有配置和实际容器绑定启动账号入口，使用与 Token 入口相同的冻结 Web image；MUST 保留原 loopback、command、network、mount 和旧日志前缀，不能新增默认端口或新配置。部署成功判据 MUST 包含真实代理 rewrite、认证、CSRF 及内部前缀隔离，不以首页或内部探针替代；真实普通账号 ACL、上传和页面业务 SHALL 在部署后作为单独的合并前门禁，不在正式部署探针窗口创建账号。

#### Scenario: 原账号容器停止且镜像较旧
- **WHEN** 原绑定与显式配置一致且符合已批准范围
- **THEN** 账号容器与其他应用一起更新到候选 Web image；实际 image ID、绑定、健康和代理身份边界通过后才可声明成功

#### Scenario: 账号入口失效或跨入口授权
- **WHEN** 账号入口不健康、绑定或镜像漂移、Token/账号身份互通，或缺失/错误 CSRF 写请求未被拒绝
- **THEN** 部署或健康检查失败，不能仅因 Token Web 可用而报告整体正常

#### Scenario: 已成功版本恢复
- **WHEN** current SHA、ready 事务、有效 v4 receipt 与已消费 manifest 一致并请求同 SHA recover
- **THEN** 恢复并重新核验双入口，不再生成业务探针；无 v4 receipt 的旧模式保持原恢复行为

### Requirement: 账号业务验收增量独立归属且不修改旧账号
业务验收 MUST 只操作本轮新对象并保留旧用户、权限、密码及会话；没有适用授权会话时 SHALL 仅使用已批准的唯一普通测试用户备选。首次改密、会话及停用仅作用于该新用户，收尾 MUST 正常清理自有对象、登出并停用，保留用户行与真实残留记录。

#### Scenario: 普通用户验收与 ACL 拒绝
- **WHEN** 使用本轮新普通用户完成上传、保存、运行及权限负例
- **THEN** 正例属于其自有新对象，负例使用另一新对象，不使用原保留责任作为写入目标；这些对象与正式部署探针分开留证

### Requirement: 首次启动后失败只经专项入口恢复旧软件
系统 SHALL 仅为本组已绑定的 starting 失败、尚无正式业务 probe 的事务提供单次 `reconcile-group2-starting`；MUST 要求闭合请求、实际专项用户批准、精确工具提交审查和 CI、完整原件及 fresh 保全事实一致。系统 MUST 保持正式安装控制器不变，只暂存绑定的隔离事故工具，不修改安装器或普通 recover 门禁，不支持自动重放、任意命令或通用失败续跑。

#### Scenario: 缺专项批准或事实漂移
- **WHEN** 请求/批准/源码/CI 摘要不符，原成功根不闭合，同 ID 已使用，或 fresh authority、DB、审计、session、文件、日志、卷、镜像及首次 startup 无法与原件核对
- **THEN** 系统拒绝推进并保留 paused/attention；不能从现场值生成新的预期或用通用旧授权替代专项批准

#### Scenario: 原软件恢复与账号停止策略
- **WHEN** 停 Control 后保全、其余应用停机、真实 idle kernel/namespace/FD 及 PostgreSQL 相同版本/RootFS/数据卷检查均通过
- **THEN** 系统仅重建旧成功 PostgreSQL 软件并启动旧 Control/Worker/Web，保留 RabbitMQ 和全部卷；account-web 保持当前容器和绑定且停止，不 restore 数据库、不迁移、不执行正式 probe

#### Scenario: 中间阶段保全失败
- **WHEN** 停 Control、停其余应用或恢复 PostgreSQL 后，完整 DB/files/log、原始 kernel/namespace/FD、容器或卷证据不符
- **THEN** 系统在下一次 phase 写入及服务变更前拒绝推进；完整阶段证据进入 receipt 摘要并由宿主与后续保全验证器重新计算，不能只在所有应用启动后汇总拒绝

#### Scenario: 第二次 startup 保全
- **WHEN** 恢复应用启动
- **THEN** 系统以新的唯一 Worker 生命周期、nonce、精确窗口和连续日志验证既有两处允许的目录 mtime；第三路径、旧内容、责任或审计变化均拒绝，普通 v4 四应用和双入口要求不变

### Requirement: 事故对账不得伪造失败部署成功
系统 MUST 先持久化独立事故原件和 receipt 并由宿主全量重算，才提交标识为 `incident_software_restore` 的旧成功 SHA transaction，backup/carry 引用也 MUST 来自此前成功事务，不能引用失败 manifest。原 current SHA、宿主 state 和旧成功 probe/receipt/consumed MUST 保持原字节；失败 manifest SHALL 原样归档为 abandoned 而非 consumed。配置 CAS 成功并保持 paused 后，attention MUST 最后清除。

#### Scenario: 持久化或控制面提交中断
- **WHEN** receipt 落盘、读回、transaction、失败 manifest 归档或配置 CAS 任一步失败
- **THEN** 系统保留真实 phase 和 attention，不自动再次停止/启动、不普通 acknowledge、不重放同事故；先只读对账并审查具体剩余动作

#### Scenario: 正常 v4 的保全根接续
- **WHEN** 事故恢复已成功且需要正常部署新候选
- **THEN** 独立 reviewer 从原参考及闭合事故链复算唯一新 snapshot：原 selection/DB、恢复后 files、追加固定 request/receipt/chain 摘要的 lineage；系统拒绝缺失/重复/伪造链或未知来源，之后重新 stage/install/plan/once 并完整验证双入口，不复用失败 manifest

### Requirement: 已批准的历史证据例外使用独立受限结果
系统 SHALL 仅为本组已绑定的旧软件恢复后中断事故提供单次事后收尾；MUST 取得明确接受历史缺口和替代来源、绑定新工具 SHA、独立审查、精确 CI、请求及动作的专项批准。结果 MUST 分别保留真实历史原件、执行路径推导、无法恢复的历史扫描与窗口、当前完整事实，不生成或宣称原严格恢复链成功。普通保全来源、原完整链和其他事故 MUST 不受该例外影响。

#### Scenario: 当前事实无法与原根核对
- **WHEN** 当前六块 DB、责任/审计/资产/session、文件内容/身份、卷、镜像、PG/schema、连续日志、唯一 startup、Worker/keeper 或原账号停止策略任一不符
- **THEN** 系统拒绝收尾；历史缺口许可不能用来放宽当前数据保全、第三路径变化或额外业务写入限制

#### Scenario: 历史扫描无法恢复
- **WHEN** 已保存的四阶段 ids/DB/files 和精确执行日志证明前置门禁走过，但历史 idle 扫描、阶段日志端点或精确应用启动窗口缺失
- **THEN** 系统只在批准的独立来源中记录缺口、推导依据及外层命令窗口来源，不能用 fresh 扫描冒充历史 raw、填充虚构字段或伪装普通 snapshot

#### Scenario: 只读核验后的控制面对账
- **WHEN** 新受限结果和唯一保全快照已持久化并经双端重算，所有当前原件仍符合绑定
- **THEN** 系统按固定顺序对账旧成功 transaction、归档失败 manifest、CAS 清除失败选择并保持 paused，最后清 attention；不停止/启动服务、不执行业务写入或 probe、不改原 state/current/成功原件，也不改原事故失败记录

#### Scenario: 后继正式部署接纳受限来源
- **WHEN** 独立 reviewer 对真实受限结果签署保全报告，并为后继候选组装 scope
- **THEN** 系统从专用闭合记录重算原 selection/DB、核验后的 files 及明确引用缺口结果的 lineage；缺失、重复、改标或摘要不符均拒绝，正常 v4 完整验收仍须另行通过

### Requirement: 本次收尾后 VM 重启使用单次旧容器启动证明
系统 SHALL 仅为已闭合受限收尾之后这一次绑定的 VM boot 提供 `recover-group2-post-finalize-reboot`；MUST 将父收尾链、独立来源 review、原成功版本/镜像/卷/显式配置、完整 stopped 容器原件、当前 boot 与 missing keeper、精确工具/审查/CI/请求/动作绑定专项执行批准。新增实现范围批准 MUST NOT 代替实际启动批准；普通 recover、原 D11/D12 及其他事故的拒绝规则 MUST 保持。

#### Scenario: 停止现场无法对齐父来源
- **WHEN** stopped 文件、材料、journal facts、日志旧前缀、卷/backing、PG 镜像 RootFS/数据目录版本、容器静态 profile 或原控制面原件不能与完整重算的父 F reference 对齐
- **THEN** 系统在创建 keeper 或启动任何原容器前拒绝；PG 停止时明确完整 DB 尚未采集，不用 fresh 文件或伪造 DB/活 Worker authority 自批新根

#### Scenario: 数据库就绪后责任发生变化
- **WHEN** 唯一 keeper 按原参数核验通过，原 PG/RabbitMQ 容器 start 并健康后，完整六块 DB、schema、PG 身份、责任/审计/资产/session 或文件不能与 F 严格核对，或只读 SQL/队列元数据发现 pending outbox、retry_wait、active Attempt/slot/cleanup、原 queued 当前代 outbox 未 published、相关 dispatch/DLQ 的总量或 ready/unacked/delayed/DLX 内部在途非零、enabled Schedule、bootstrap/admission 修补候选、retention 年龄/数量/idempotency 候选、到期 dead-letter hold、GC reservation/binding/staged/deletion/retry lease/磁盘 orphan 或 Worker journal recovery/cleanup 可消费工作，或这些来源无法完整核对
- **THEN** 系统保存该阶段完整原件和真实状态并停止，Control/Worker/Web 不启动；不修改旧行/消息、恢复备份、迁移、清理责任或放宽比较

#### Scenario: 本次新 boot 下启动同一旧 Worker
- **WHEN** 仅一次 start 原 Control/Worker/Web 的已绑定容器 ID，account-web 保持原 stopped
- **THEN** 系统保持原 ID/image/完整 profile，核新的 StartedAt 在唯一窗口内、零 restart、一份新 nonce 的完整 preflight/cleanup/residue 和连续日志；新 keeper/Worker PID、namespace/cgroup 身份须来自当前 boot 的实际内核证据，同 boot 内保持连续，不盲复用旧 boot 身份比较器或允许自动 recreate

#### Scenario: 缺少运行副作用或后续停留的完整依据
- **WHEN** keeper 脚本实际 raw/hash/mode/owner/来源未独立冻结，原 Rabbit 版本/插件下的内部在途统计或已证明覆盖的总量缺失且无不适用证据，恢复窗口超时，或原行/策略导出的最早自然变更时间不能覆盖请求冻结的紧接后继验证期限
- **THEN** 系统拒绝相应动作或成功结果，保留最早变更时间及候选原件，不把缺字段视为 0、不假定父四工具摘要覆盖 keeper 脚本、不以窗口内通过宣称无限期安全；后继必须在证据有效期内 fresh 核对，不因正常远期保留策略直接拒绝，也不得为放行改配置或增加定时停止/续跑

#### Scenario: 本次启动产生目录元数据变化
- **WHEN** 本次完整 startup 和新生命周期证明通过且所有旧 DB/责任/审计/排队记录不变
- **THEN** 系统只允许 runtime 根及 journal/sandbox-recovery 两处目录 mtime 在本次窗口中单调变化，并保留旧日志前缀、内容、权限、owner、卷和其他文件身份；第三路径、第二次本轮 startup、旧 nonce 或额外业务消费均拒绝

#### Scenario: 执行动作或落盘中断
- **WHEN** 工具暂存、keeper、PG/Rabbit、应用启动、阶段采集、result/receipt/chain 落盘或宿主读回任一点中断
- **THEN** 系统保留此前落盘的操作意图、实际阶段和已有原件，不自动 retry/resume、回退或补造；父事故/收尾/boot 唯一目录阻止换请求 ID 重放，旧 state/current/transaction/config/attention、安装字节、成功与失败原件不变

### Requirement: 重启后来源只能由父链及本次证明唯一导出
系统 SHALL 仅通过专用 `group2_post_finalize_reboot_v1` 来源接纳本次成功结果；MUST 先严格重算原 F 链及原参考，再验证本次逐阶段证据，唯一导出父 selection/DB、已证明的本次 files 和追加 request/receipt/chain 摘要的 lineage。新来源 MUST 由独立 reviewer 冻结，绑定本次工具/controller 与后继 exact scope；原父工具绑定和旧来源规则不得改写。

#### Scenario: 后继候选承接新的保全来源
- **WHEN** preview 安装、规划、选择或最终切换读取本次来源
- **THEN** 系统重算完整父链和本次链，拒绝缺失/重复/摘要不符、fresh 自批、改标普通 snapshot、父 F 冒绑新工具或未经证明的其他 boot；原 state/transaction 和 F 成功不重做，后继仍须 live compatibility、正式四应用/双入口/真实 Chrome 及原合并门禁，纯构建缓存不替代这些门禁
