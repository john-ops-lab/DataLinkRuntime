# Worker Pool 路由

可为停止态 Adapter 准备 Pool 路由。生产继续使用固定 Worker。新请求解析为 Pool 时，服务端在建立执行或派发责任前拒绝；执行入口返回 HTTP 409 和 `pool_routing_not_enabled`。已受理的幂等请求按原入口的权限和幂等合同读回原责任。

## 配置路由

管理员通过 `/api/worker-environments` 登记不可变环境，在 `/api/worker-pools` 创建规则，显式授权 Worker 身份加入。规则绑定环境类、管理员标签的等值合取和 placement scope。修改规则生成新 revision；成员的 placement、材料及凭据授权独立维护。成员预览缺少独立环境证据时显示环境未知，此时无法确认容量资格。

显式更新 Adapter route 携带当前 `expected_route_version`；Pool 管理写入使用 `expected_version`，版本冲突后重新读取。fixed 保持原 Worker ID，无自动转移。Pool 草稿绑定精确的 Pool 和 revision，兼容字段 `runtime_worker_id` 返回 null；显式 route 与旧 Worker ID 冲突返回 422。同部署 Clone 固定或 Pool 路由的 Adapter，副本也从停止且未绑定状态开始，须显式选择新路由。模板和导入保持停止，不可移植的引用须显式重绑，同名 Pool 不授予权限。

## 核对路由来源

| 操作 | 来源及保留的事实 |
| --- | --- |
| 手工、定时、Webhook、首次订阅激活 | `adapter`，接受或激活时的绑定。 |
| 单条 T2、初始业务批次 | `activation`，使用原激活，之后修改 Adapter 不影响此责任。 |
| 自动 Retry | 沿用原 Execution 及冻结路由。缺少可证的历史 Worker 时保留原责任；Retry 与新 Outbox 均不得使用可变目标。 |
| 原配置事件 Replay、repair | `original` 或 `repair`，保留原路由；repair 仅替换允许修复的 Revision。 |
| 显式 current-config 事件 Replay、普通 Execution Replay | `current-config`，取当前绑定。事件入口仍要求停用并清空其他未完责任。 |
| 依赖检查及其 Replay | `dependency-check`，Replay 须有原固定 Worker。 |
| 升级前历史 | 已记录固定目标时为 `legacy`；未证实的环境保留 `unknown`。 |

配置路由、当前派发目标和实际 Attempt 节点分别查看。原顺序、失败屏障及重放限制继续有效，含已闭合的 Kubernetes completed。

## 离线升级与启用

1. 备份数据库、原卷及私有声明和意图文件。按固定节点维护流程停止 Control 写入，排空并对账。保留原权威、epoch、身份、回执和镜像摘要。[固定 Worker 运维](remote-worker-wave1a.md)
2. 使用 owner 离线从 `0090_issue129_worker_cutover` 升至 `0091_issue129_pool_routing`。支持 W1 发布提交 `347be42970a765fc08557528991134d60c79dcae` 及已接受镜像来源 `bbfedfa93f37f9c008084085e8bbb7998fba8897`。J 部署先沿原有 J→0090 路径升级。保留闭合的前序链。
3. 使用 owner 连接，依次运行 `python -m dlr.control.control_identity_init_v4` 的 `check`、`apply` 和独立 `observe` 子命令。三次传入相同私有 `--source`、`--target`、`--intent`，并按 previous、root、origin、ancestor、initial、earliest 提供前序 source/intent 对。新增一对为 `--earliest-source` 和 `--earliest-intent`。应答不明时观察原意图；独立读回 `committed_current` 后再以运行角色恢复服务。
4. 历史 Execution、Activation 使用各自已记录的固定 Worker 回填，缺乏固定目标的历史 Execution 保留未分类。本次升级开放停止态草稿。生产 Pool 接收待后续统一完成容量对账、派发前预留、有界等待及故障恢复后切换。
