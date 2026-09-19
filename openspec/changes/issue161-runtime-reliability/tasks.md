此清单记录候选冻结时已经完成的工作，以及后继真实验收暴露的本组必要修订。固定入口真实验收、精确 HEAD CI、部署与合并结果随后记录在 PR 交付证据中，并在用户最终验收/归档时回填清单。

## 1. #135 A — Worker metadata 独立检查点

- [x] 1.1 核对精确集成 HEAD、工作区及本规划，补 Worker Check/Index 声明；以 migration 0031 的名称、字段顺序和表达式逐项比较，确认不新增 migration。
- [x] 1.2 使用真实 PostgreSQL 完整迁移库和 SQLAlchemy inspection 比较上述两个对象；新增针对 metadata 漂移的回归，验证无重复对象、无误删，原 Worker 数据不变。
- [x] 1.3 运行相关 model/migration 测试、Ruff/format/Mypy 适用检查及 `git diff --check`，记录命令、预期、实际与 SHA，独立提交 #135 A 后继续。

## 2. #135 B — canonical cancellation 独立检查点

- [x] 2.1 统一 queued/retry_wait、claim 前 cancel flag、active 协作取消的 error_code/last_error_code 为 execution_cancelled；以 API/detail fixture 和既有取消合同测试证明 status/decision reason 不变、历史终态不批量改写。
- [x] 2.2 用真实 PostgreSQL 覆盖未 Claim、retry_wait、running、重复请求以及 cancel/Claim/terminal 竞争的两种先后顺序；断言 canonical code、原 Slot/Admission/Lease/cleanup/fence 不变量与一次释放。
- [x] 2.3 运行相关取消/claim/result 回归和适用静态检查，记录独立验证证据并提交 #135 B；不得将第二组指定queued取消UI包含到本检查点。

## 3. #134 — 持久公平扫描独立检查点

- [x] 3.1 增加有界扫描 cursor/active ID索引的增量迁移和 ORM，按设计实现固定 upper_id 轮界与短事务候选保留；验证 fresh/基线上升级、空集合、删除尾项、最多两次有界查询及重启不重置。
- [x] 3.2 将恢复拆为逐行干净事务和封闭行级校验错误分类，保留原 terminal 服务；以 retry_policy_invalid 等确定性坏快照验证 rollback 后业务lease/状态/Slot/Admission不变，未知异常仍上抛。
- [x] 3.3 真实 PostgreSQL 测试 limit=1 最前坏行、坏行=B、坏行>B、持续新到项；断言正常后继在设计tick上界内获得处理，不删坏行、不伪造终态、不无限填批。
- [x] 3.4 测试保留候选后进程重启、两个reconciler并发、renew/result/recovery竞争；以持久游标重读、单terminal/单retry/单释放断言核对公平性和锁顺序。
- [x] 3.5 构造同tick due retry和expired hold，确认坏行不阻断它们；注入真实DB断连及未知程序异常，确认错误可见且没有虚假收敛；检查日志仅含固定码与非敏感ID。
- [x] 3.6 运行针对性 Backend 测试、迁移/静态检查，记录 #134 独立SHA和证据，提交后才进入 #152 A。

## 4. #152 A — 正常容量背压独立检查点

- [x] 4.1 在固定Pika版本上实现 SelectConnection transport、每槽一次性consumer与最小ticket状态；用确定性调度测试证明 receiving+cancelling+working<=S、prefetch=1、每ticket只释放一次、pool无嵌套无界排队。
- [x] 4.2 实现 CancelOk后Claim、journal durable后ACK-send receipt、receipt后start；实现epoch校验、真正IO deadline和有界abort/重连。用事件驱动测试覆盖注册/CancelOk丢失、heartbeat仍通的Broker主动Basic.Cancel、其与CancelOk/channel-close/任务完成交错、慢Claim/journal、旧epoch回调、shutdown与槽释放竞态；断言epoch/tag隔离、每ticket至多一次工作提交/释放，不把socket timeout当RPC deadline。
- [x] 4.3 真实RabbitMQ 4.3.5+Worker+Linux Sandbox运行S=1/2且消息远多于S、首批持续超过默认300000ms consumer timeout；采集连接/consumer/ready/unacked/失败delivery计数/DLQ与槽数，证明正常容量无重投、无关连接循环或心跳/取消阻塞。
- [x] 4.4 分别执行计划自然重试、真实HTTP Webhook容量等待、手动排队；每项核对原Execution ID、Attempt、输出、generation、Admission，释放容量后原记录继续，不能用新任务替代。
- [x] 4.5 真实故障回归包括Control/Broker断线与重启、idle consumer在heartbeat健康时被Broker主动取消且队列恢复后重新接收、ACK丢失、journal失败、取消注册边界、重复消息、迟到结果、原DEFER delayed retry；断言单active Attempt/fencing、资源有界、容量与故障日志分类且无凭据。
- [x] 4.6 运行Worker相关完整回归、静态检查并独立记录 #152 A 实现SHA及真实Broker证据；经指定独立Review修正问题后提交检查点，再进入B。

## 5. #152 B — 原Execution人工处置独立检查点

- [x] 5.1 新增处置审计增量迁移、schema、幂等键/请求hash和独立计数；真实PostgreSQL升级保留旧Incident且不补造处置，测试唯一约束、同key重试/冲突、只读不计数和DLQ重复观测分离。
- [x] 5.2 实现无commit cancellation helper和事务性dispose服务，冻结统一锁顺序、终态hook与资源责任；真实并发测试证明审计/mutation原子、取消与Claim/Result/Recovery无锁反序、无重复Slot/Admission/Lease释放。
- [x] 5.3 实现资格与Outbox决策表：当前queued、active、cancel_requested、retry_wait、终态、旧/未来代、身份不符、missing/corrupt、pending/leased/published/settled及容量不足；每行有独立预期code/副作用断言，验证只必要时增加代次且不新建Execution。
- [x] 5.4 实现原冻结材料验证，覆盖JSON/null/none、managed-files原Lease与hash/size/ordinal、版本/Worker/resource/retry/builtin/credential引用；真实文件删除/GC与提交竞态测试证明不替换历史快照，未知引用拒绝且日志/响应无Secret。
- [x] 5.5 实现POST处置和审计分页、增量reliable-detail能力字段；API测试覆盖401/403/404、跨Adapter、伪造actor、读后被Claim/取消/终结/材料失效，后端提交重检安全拒绝或幂等反馈。
- [x] 5.6 在写UI前读取项目Ant Design skill并查询5.29.3对应API；在ExecutionHistoryPanel加入恢复/终结、资格原因、观测/处置计数与结果，中英文及当前权限传递；Web测试覆盖重复点击同key、409刷新原记录、active协作取消说明和键盘操作。
- [x] 5.7 按结构化x-death修正reason分类；测试delivery_limit/rejected/expired/maxlen、历史链、不同queue、畸形/缺失headers，真实Broker验证非delivery-limit自动流程仍按原合同运行，不新增自动delivery-limit恢复。
- [ ] 5.8 真实Chrome经固定入口完成旧Incident恢复与合法终结，含read-only拒绝、终态反馈和详情刷新；逐条核对原ID/输出/Attempt/代次/审计/资源，保留实际截图与API/DB关联证据而非仅UI组件测试。
- [x] 5.9 运行Backend/Web相关完整回归和静态检查，保留#152 B各子提交SHA，独立Review修正资格、锁/幂等、材料与权限问题；与A证据分开记录。

- [ ] 5.10 完成详情输出可读性的最小 CSS 修复及独立 Review；原生 wheel/尾部几何验证普通/长/截断 JSON、空输出、日志、多 Attempt/Incident、桌面/窄屏，已滚动 Drawer 内键盘 Popconfirm/权限/epoch/幂等不回归；保存精确旧源红例与新源绿例，固定 Chrome 验收仍由 5.8 单独记录。

## 6. #152 B — 旧Incident保全升级配套

- [x] 6.1 在默认空闲门禁之外实现显式候选绑定carry-forward清单、运行责任manifest及私有参数入口；控制器单元测试证明无通配忽略busy、仅允许指定同机制向前兼容责任，公开diff无私有地址/ID/凭据/备份。
- [x] 6.2 实现停Control/Worker后的双重复核和never-claimed cleanup派生分类；合成测试覆盖queued/terminal pending无Attempt、真实deferred且journal完整、journal丢失、未知进程、预检后Claim，不修改pending为completed、不取消原queued。
- [x] 6.3 将Execution/Attempt/Slot/Incident/Outbox/Admission/Input Lease/Hold及runtime/journal指纹加入独立保全验证；真实带数据升级证明备份/迁移前后原字段一致、所有卷和Broker责任保留，deferred由真实Worker回执收敛。
- [x] 6.4 对控制器及双语部署说明做独立Review，运行 `python3 -m unittest discover -s tools/local-preview/tests -v`、`bash -n tools/local-preview/deploy.sh`及原门禁回归；通过后才由集成owner安装已审查控制器，不绕过attention/CI/历史/镜像/Sandbox检查。


- [ ] 6.5 按 design 第 9 节实现显式 `audited-web-same-schema-v1` / manifest v3 和闭合 terminal 选择；验证原 v2 shape/audit-empty 行为不变，unknown mode/version/schema/键、空或重复选择以及 terminal 未显式选择 cleanup 的反例在 plan 写出及停服前被拒绝。
- [ ] 6.6 仅对 design 9.1 精确路径实现完整 Git commit/tree/raw diff 重算和 source_diff 绑定；以临时真实 Git 仓库验证允许 CSS＋配套修改与两个原 `100755` 脚本保持 mode 的正例，未知/backend/migration/依赖/CI 路径、A/D/R/type/mode/symlink/submodule、原脚本 chmod、其他路径新增 executable 权限、改对象和 plan 后候选漂移均拒绝，plan/select/switch 停服前都实际重算且控制器/旧新 migration graph 摘要仍全绑定。
- [ ] 6.7 在原十三张责任表外为 v3 捕获完整十七列 audit 的真实 PK/有序整行 hash/count，核对全部 audit 集与 terminal/Incident/Outbox/请求/actor 关系及明确终态预期；真实 PostgreSQL 正例与逐列/同数换行/隐藏列/类型等值/额外或缺失 audit/inflight/错误结果负例全部通过，不用 observer 窄投影或 count 代替。
- [ ] 6.8 保持 queued/cleanup 资格和所有锁序、文件/kernel/namespace/FD/卷门禁；验证 terminal/cleanup 合法交集、取消后的零 Attempt pending 必选且只派生不适用、剩余同 Adapter queued charge 不归零、未知 journal/新 Claim/新 audit/计划至停写间变化拒绝；原全部 controller 回归继续通过。
- [ ] 6.9 更新双语 local-preview 操作合同并对精确实现做独立 Astra ultra Review；运行 controller 全量 unittest、Python 编译、shell 语法、OpenSpec strict 与 diff 检查；隔离真实 PostgreSQL 仅验证合成 baseline/重复采集、pg_dump/pg_restore list、同 head no-op Alembic、十四表/实际 PK/inventory 及 race 拒绝，记录 LOCAL_FAST 证据，不伪造 Control/kernel 或另建完整验收环境；固定环境的实际 stop/kernel/卷/镜像/probe 证据只在 6.10 提供。
- [ ] 6.10 由集成 owner 在最终 HEAD CI/独立 Review 通过后官方安装并使用 fresh v3 完成固定环境同 schema 更新；核对完整审计及全部原终态/queued/cleanup 原责任、实际镜像与 Ready，probe 仅允许可关联自身增量，再以真实 Chrome 只读检查原结果，保留旧 v2/原失败证据；任何漂移保留 attention，不清审计、不再次处置或绕过门禁。

## 7. 第一组最终head门禁与交付

- [ ] 7.1 维护逐项证据索引，覆盖#135 A/B、#134原始及新增公平验收、#152 A/B原始与新增验收；每项关联实现SHA、命令/环境、独立预期、实际结果和未验证项，确认没有删减原#130合同或把桩测试当真实Broker/DB/UI。
- [ ] 7.2 最终head执行 Backend Ruff/format/Mypy/full pytest、Web lint/typecheck/test/build、完整fresh及带旧数据Alembic检查、Compose smoke、适用Sandbox/Broker/权限/数据保护Gate；跑 `openspec validate issue161-runtime-reliability --type change --strict --no-interactive`和`git diff --check`，所有失败先修复。
- [ ] 7.3 对精确最终head完成指定Astra ultra独立代码Review；Review问题修复后重新核对受影响测试和head，清除本次无用临时文件，不冒充官方CI/Review。
- [ ] 7.4 由REMOTE_RELEASE集成owner创建第一组唯一PR，文字使用关联Issue而无自动关闭关键字；执行私有预览控制器select/status，等待精确HEAD CI通过再部署，必要时使用已审查carry-forward清单；记录原Execution真实恢复验收和实际部署镜像SHA。
- [ ] 7.5 仅在整组技术/发布门禁通过后按用户已有授权合并，核对合并SHA、最终部署SHA及必要运行回归；依用户收窄后的范围，第一组完成即停止，不进入后续组。PR存在不代表合并，镜像启动不代表真实执行成功。
- [ ] 7.6 单独列出用户最终体验验收与Issue关闭状态；#135/#134/#152在所需用户手工验收前保持开放，#136保持既有合并归档，未跑项不得勾选完成，不把非阻塞体验验收误写为门禁通过证据。
