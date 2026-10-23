# B4 审查与验收记录

**状态：实施中，尚未通过本批验收。** B3 已关闭；本批按已批准设计接入 Kubernetes、全量批次、显式合并及专用“放弃完成确认”。接口、单元测试或隔离目标就绪均不代表来源到业务的交付完成，公共可启用来源仍按 B5 门槛控制。

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

初次运行有一条测试断言的空正则匹配提示，移除无效参数并补入审查发现的两个用例后，最新 12 项通过且无该提示；计数不累加重复运行。隔离目标初始化的本机运行环境、私有配置传递与数据库定位首败另有私有记录，未改写为产品首轮成功。目标容器已更新到包含旧 WATCH 防护的候选脚本，逐哈希核对、空库及健康检查只证明 fixture 就绪；**尚无 Kubernetes→DLR→Worker→该目标的业务验收结论**。

## 候选代码的独立审查

来源传输与身份快照独立审查通过：冻结文件哈希匹配，定向 28 项检查通过，含两项真实 PostgreSQL 的 runner/T1 集成；另外 10 个非法 namespace/GVR 输入均在原生请求前拒绝。原作者 42 项与这些复验存在重叠，不累加为不同用例数量。原始体预算不是 RSS 硬上界，read1 间的截止时间检查也不宣称能够硬取消 DNS/操作系统阻塞。

管理/API 独立审查通过：原 18 项 PostgreSQL 测试与修订后 3 项受影响复验通过；审查发现 Batch 详情 DTO 的 RV 2048 上限与来源/核心 4096 UTF-8 字节不一致，已统一并验证长 opaque RV 完整读回。最终作者 19 项覆盖成功闭合 completed 禁止新 replay、旧请求在配置升版/featureoff 后只读回、权限与专用完成放弃。双语文档及七份最小规划修订另经独立审查，B4/B5 任务仍未提前勾选。

事务核心原冻结 20 项独立 PostgreSQL 测试通过；继续代码审查发现，已执行失败的 WATCH 在人工 replay 尚未派发时可能被错误合并。现已修正为检查任意历史 Delivery 的 Execution 关联；旧逻辑在进程内替换下复现失败，修后作者 23 项通过，证明显式补处理责任、原 Envelope 和账本保留。completed 落账及 T2 另复核 started 最新业务成功；公开 replay 在 ready 与已材料化两个窗口均拒绝重开未排空责任。事务核心独立审查通过，修订后组合复验包含上述回归。

最终受影响后端组合为 225 passed / 11 skipped（89.36s），覆盖全部 Issue #219 测试与订阅兼容测试，含独立目标用例在常规 conftest 下的集成；跳过的是未配置专用连接的可选真实 Kafka 故障测试，本批没有借此重跑 B3 已关闭的 Kafka 故障矩阵。全后端 Ruff 与 439 文件格式检查通过，mypy 214 源文件通过，核心窄修另经对应类型/格式检查。OpenSpec strict 和公开暂存内容检查通过；这些静态和事务结果不替代部署及业务验收。

## 真实来源的只读协议代表

独立 K3s API server 实读版本为 `v1.35.9+k3s1`，没有 agent/Node；所建 Pod 与 replicas=0 的 Deployment 是实际 API 元数据资源，不声称 Pod 工作负载已经运行。专用 ServiceAccount 仅有本任务 namespace 内 Pod/Deployment 的 get/list/watch 权限；真实配置和凭据只保存在私有配置中。

候选连接器使用 SDK 35.0.0 / urllib3 2.8.0 完成无接管的 LIST 与短 WATCH 连接测试。默认每页 16 项实际读到 `[16, 1]`，共 17 个唯一 UID、两页 collection RV 一致，selector 外的另一 Pod 未进入结果。原始应用缓冲峰值 27,609 字节，显式关闭 pool 后原生 pool 已关闭；FD 首尾均 4、线程首尾均 1。这些是单次诊断及关闭证据，不是运行进程 RSS 硬上界。

另主动从真实 API 请求旧 `resourceVersion="1"` 的短 WATCH，得到实际 410 并转为有界 `source_history_gap`，原生资源已关闭。此项只证明真实旧位置的协议分支：没有修改或回退 DLR checkpoint，不能说平台保存位置自然过期、已补齐历史或完成平台恢复。上述来源只读操作未创建 DLR Batch/Receipt/Execution。容器网络内的 server 入口另通过严格 CA/hostname TLS 1.3 核对；不以握手成功代替来源或目标验收。

## 待完成门槛

批次/预留/完成冻结与失效、abandon 两向竞争、幂等与账本/GC、WATCH 恢复及合并、API 权限/运行锁、真实来源代表路径和独立目标读回仍按 tasks 与 validation 逐项推进。B4 tasks 尚未勾选；后续还须独立代码审查、必要检查和精确来源记录。B5 再完成 Kubernetes 产品界面与最终集成，最终统一 PR 的远端精确 HEAD CI 不由本记录提前声称通过。
