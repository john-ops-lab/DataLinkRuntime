# B4 审查与验收记录

**状态：B4 已通过验证及独立审查。** 本批交付 Kubernetes 后端、全量批次、显式合并及专用“放弃完成确认”，并以真实 Pod API→DLR→Worker→独立目标及停止后 RV 续接完成代表验证。公共可启用来源仍只有 Kafka；Kubernetes 产品界面、Deployment 真实业务及最终集成由 B5 完成。

## 已固定的实施边界

- 来源客户端固定 `kubernetes==35.0.0`、`urllib3==2.8.0`，仅声明 Pod `core/v1` 与 Deployment `apps/v1` 的逐项验证范围。
- 配置版本保存独立、非用户字段的来源身份快照，包含规范 HTTPS origin、实际 CA 证书集合摘要与 scope 指纹。运行时核对同一份 CA bytes，再固定给 SDK；旧信任快照不能用当前 bundle 静默补齐。同凭据引用的 token 值轮换不变更 scope。
- 保留既有兼容行为：API 可保存已知候选来源的停用配置并进行受控诊断；公开启用仍须 capabilities 允许。不能将可保存或诊断成功写成 Kubernetes 已交付。
- LIST/WATCH 仍进入统一 T1/T2/Admission/Outbox；控制预留、批次资格、合并与人工完成结账由同一事务核心管理，来源 SDK 不直接创建 Execution。
- “放弃完成确认”保留冻结与原失败事实，只终止旧完成链未来执行许可；不代表目标成功，也不回滚远端副作用。新批次的目标清理由持久 generation 与控制幂等共同保护。

## 普通 Adapter 与隔离目标候选

新增普通 Python 样例将闭合 Envelope 发送到独立 PostgreSQL 目标，不在 Adapter 内启动 LIST/WATCH。目标按订阅与 scope 隔离所有权，持久化 batch generation、UID 成员、实际对象及每个控制的幂等结果。完成前同时检查成员数量与当前目标行内容，只清理本范围的旧 generation；failed 保留旧成员排空能力，永久取消该批清理资格。

目标定向测试使用单独的临时 PostgreSQL 数据库，不依赖 DLR 应用表或迁移；最新 12 个不同用例通过，包含：

- 缺失成员或已消失的成员业务行拒绝完成清理，scope 外行保留。
- failed 后成员可继续写入，但不能恢复旧完成资格。
- 新 started 生效后迟到旧 completed 被拒绝；已提交旧控制的重试不重复清理。
- 独立审查指出携带旧 Batch 的 WATCH 仍可误写新代次；已加 generation 校验，并分别验证旧 MODIFIED/DELETED 不覆盖或删除新行。无 Batch 的 WATCH 没有代次事实，不能从不透明 RV 推断任意迟到更新次序；样例明确该业务边界。
- 同名新 UID 不受旧 UID 删除影响；同源事件 ID 不同内容拒绝。
- 普通样例经真实 HTTP 分别执行 started、成员、completed、failed；目标提交后丢失响应，独立 SQL 仍读到一次持久结果，相同输入补处理不重复副作用。

初次运行有一条测试断言的空正则匹配提示，移除无效参数并补入审查发现的两个用例后，最新 12 项通过且无该提示；计数不累加重复运行。隔离目标初始化的本机运行环境、私有配置传递与数据库定位首败另有私有记录，未改写为产品首轮成功。目标容器更新到包含旧 WATCH 防护的候选脚本后，逐哈希核对、空库及健康检查仅作为 fixture 就绪门槛；后续真实业务结果另见下文。

## 候选代码的独立审查

来源传输与身份快照独立审查通过：冻结文件哈希匹配，定向 28 项检查通过，含两项真实 PostgreSQL 的 runner/T1 集成；另外 10 个非法 namespace/GVR 输入均在原生请求前拒绝。原作者 42 项与这些复验存在重叠，不累加为不同用例数量。原始体预算不是 RSS 硬上界，read1 间的截止时间检查也不宣称能够硬取消 DNS/操作系统阻塞。

管理/API 独立审查通过：原 18 项 PostgreSQL 测试与修订后 3 项受影响复验通过；审查发现 Batch 详情 DTO 的 RV 2048 上限与来源/核心 4096 UTF-8 字节不一致，已统一并验证长 opaque RV 完整读回。最终作者 19 项覆盖成功闭合 completed 禁止新 replay、旧请求在配置升版/featureoff 后只读回、权限与专用完成放弃。双语文档及七份最小规划修订另经独立审查，候选阶段没有提前勾选 B4/B5 任务。

事务核心原冻结 20 项独立 PostgreSQL 测试通过；继续代码审查发现，已执行失败的 WATCH 在人工 replay 尚未派发时可能被错误合并。现已修正为检查任意历史 Delivery 的 Execution 关联；旧逻辑在进程内替换下复现失败，修后作者 23 项通过，证明显式补处理责任、原 Envelope 和账本保留。completed 落账及 T2 另复核 started 最新业务成功；公开 replay 在 ready 与已材料化两个窗口均拒绝重开未排空责任。事务核心独立审查通过，修订后组合复验包含上述回归。

`ab3a924` 候选的受影响后端组合为 225 passed / 11 skipped（89.36s），覆盖全部 Issue #219 测试与订阅兼容测试，含独立目标用例在常规 conftest 下的集成；跳过的是未配置专用连接的可选真实 Kafka 故障测试，本批没有借此重跑 B3 已关闭的 Kafka 故障矩阵。全后端 Ruff 与 439 文件格式检查通过，mypy 214 源文件通过，核心窄修另经对应类型/格式检查。后续真实来源发现的 TypeMeta 窄修采用下文定向复验。OpenSpec strict 和公开暂存内容检查通过；这些静态和事务结果不替代部署及业务验收。

## 真实来源的只读协议代表

独立 K3s API server 实读版本为 `v1.35.9+k3s1`，没有 agent/Node；所建 Pod 与 replicas=0 的 Deployment 是实际 API 元数据资源，不声称 Pod 工作负载已经运行。专用 ServiceAccount 仅有本任务 namespace 内 Pod/Deployment 的 get/list/watch 权限；真实配置和凭据只保存在私有配置中。

候选连接器使用 SDK 35.0.0 / urllib3 2.8.0 完成无接管的 LIST 与短 WATCH 连接测试。默认每页 16 项实际读到 `[16, 1]`，共 17 个唯一 UID、两页 collection RV 一致，selector 外的另一 Pod 未进入结果。原始应用缓冲峰值 27,609 字节，显式关闭 pool 后原生 pool 已关闭；FD 首尾均 4、线程首尾均 1。这些是单次诊断及关闭证据，不是运行进程 RSS 硬上界。

另主动从真实 API 请求旧 `resourceVersion="1"` 的短 WATCH，得到实际 410 并转为有界 `source_history_gap`，原生资源已关闭。此项只证明真实旧位置的协议分支：没有修改或回退 DLR checkpoint，不能说平台保存位置自然过期、已补齐历史或完成平台恢复。上述来源只读操作未创建 DLR Batch/Receipt/Execution。容器网络内的 server 入口另通过严格 CA/hostname TLS 1.3 核对；不以握手成功代替来源或目标验收。

## 精确候选部署与真实链路首败

候选 `ab3a9246aff2eb58d6dd52ba7c0ed2a677dc2e68` 已按精确 Git archive 构建并部署，开发数据库前向升级到 `0048`。核对 Control 358、Worker 307 个文件哈希与 75 个运行依赖锁版本，新增 Kubernetes/urllib3 的项目依赖元数据也已重建。Web 源码无变化，继续核对原 B3 构建的两个 98 文件产物。固定六个卷、挂载、Token/Master Key、其他容器，以及原数据库的 schema、135 条终态执行和 Adapter 数量保持不变。

首次离线构建中，`uv sync` 因本地 wheel 的安装来源与锁中 registry URL 不同而尝试重新下载，离线失败；没有部署该候选。随后使用与锁哈希匹配的五个官方 wheel、离线 editable 元数据重建、完整已安装版本逐锁核对和 pip check，通过本机候选构建；不宣称公开 Dockerfile 的在线构建或离线 `uv sync` 已通过。

真实链路脚本最初两次在创建订阅前退出，分别为本机 `host:port` 绑定拼接和 host 只读客户端导入缺少必填运行配置；修正入口和前置依赖后保留原失败记录。首次真正接入来源时，真实 LIST 成员只包含 metadata/spec/status，省略了可选 TypeMeta；候选校验错误地要求逐成员存在 kind/apiVersion，导致 `source_object_invalid`。失败批次与来源错误已保存，通过失败控制收敛后订阅已停用、原生关闭且排空，预留归零；没有成员或 completed，不回写该批次为成功。

该兼容性窄修仅允许已验证 typed LIST 页面中的成员省略一个或两个 TypeMeta 字段，完整保存原 payload；显式错误、空值/null、WATCH 独立对象及顶层 LIST 的类型验证维持严格规则。官方 TypeMeta 声明、serializer/WATCH 编码路径与实际 raw LIST 共同支持此边界，不能将其泛化为 WATCH 豁免。修后作者 56 项来源测试通过（2.20s），独立按影响选取 15 项通过（2.00s），包含真实 PostgreSQL runner 原样保存缺省成员、原始 16+1 两页解码与错误值拒绝；其余 41 项未重复，Ruff/格式/两模块类型检查通过。

## 修订候选的真实 Pod 业务与恢复

精确提交 `326dca6d8d7581d6d82402d2537ceb41249eb83d` 复用同一环境和目标，升级前完成备份读回及六卷/连接目标/密钥等值检查；开发数据库保持 `0048`，Control/Worker 文件哈希、实际导入路径、75 个锁版本和原库保留再次核对通过。公开 capabilities 仍只 Kafka，公开 Kubernetes enable 返回 `409 source_not_available`，没有激活或接管；仅在短命内部进程经自有 Adapter/订阅/namespace 绑定后启用候选，没有修改运行中 HTTP 服务的公开注册。

真实 API 经 `[16, 1]` 两页 LIST 接管 17 个预先创建的唯一 UID，selector 外 Pod 没有进入结果。started、17 个成员、completed 共 19 条责任全部经 RabbitMQ、实际 Worker Execution/Attempt 和 published Outbox 成功；逐个核对源 UID 集合、原始 payload、事件身份及目标对象/成员/控制结果哈希。独立 PostgreSQL 目标为 17 个对象、17 个成员、19 份幂等结果，每个对象写入一次；目标批次已业务完成，不以 Control 执行灯代替目标读回。

随后只修改一个自有 Pod 的 annotation。对应 UID 与精确 patch RV 的 MODIFIED 责任成功，目标保持 17 行，选中对象写入次数变为 2，其他对象仍为 1，payload/hash 与该变更一致，目标幂等结果增至 20。停用、原生关闭和排空后，普通 SavedInput 保持原值，active listener 与应用缓冲均归零。

关闭期间再次修改同一 UID 的另一 annotation，20 条原责任集合保持不变。显式内部启用后使用同一 epoch、新 activation 从持久 RV 续接，取得该精确变更的成功 WATCH；原 Batch 仍为同一份，17 个 LIST Receipt ID 集合没有变化，总责任 21，目标仍 17 行且选中对象写入次数为 3，完整 payload/hash 已核对。再次停用后 native/buffer 归零。此续接是第二个 WATCH 代表，复用此前完整 Attempt/Outbox 基线，不宣称又执行了一轮独立全量验收。

B4 真实业务范围为 Pod API 元数据；Deployment 的真实业务路径及 Kubernetes 产品界面仍由 B5 完成。两向 T2 竞态、满额专用放弃和 30 天留存沿本批确定性 PostgreSQL 用例，后续实际满额/UI 代表路径按既定 B5 范围执行。没有把主动旧 RV 的诊断 410 写成平台位置自然过期或历史补齐，也没有提前声明远端 CI、发布或用户验收通过。

## 独立结论与后续门槛

来源、事务核心、管理/API、目标样例、规划修订及真实数据均经独立审查，通过时的源码与证据哈希已留在私有审查记录。独立数据复核重新计算了实际 CA/GVR/namespace/selectors 的 scope、17 个来源 UID/目标内容与成员哈希、21 个不同事件身份及目标事件哈希，并逐条对齐首轮 20 份 Execution/实际 Worker Attempt/published Outbox。续接复核保留同 epoch/new activation、唯一原 Batch、无新 LIST 与目标写入次数；没有把离线审查说成重新调用 API。B4 六项任务据此关闭。

B5 继续完成 Kubernetes 产品界面、声明的 Deployment 真实业务、部分批次故障、满额/未派发补处理的专用放弃、迟到旧 completed 防护及受影响公共路径。最终统一 PR 的远端精确 HEAD CI、发布与用户验收不由本记录提前声称通过；不会自动合并。
