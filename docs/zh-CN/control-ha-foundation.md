# 双 Control 基础部署

W3 第一批提供同镜像双 Control 注册、分层健康与集群接收容量。双节点通过同一 PostgreSQL 权威核对部署、镜像、schema 和认证材料，每次启动分配新的进程身份。单机沿用原来入口和原卷容量观测。共享存储、来源接管和完整基础设施 HA 按后续波次启用。

## 升级与准入

1. 以 W2 发布 `9b3347578b1644706731e5f7c5c1461385b71741` 和 `0093_issue129_worker_placement` 为来源。按既有 [Control 停机换镜像](remote-worker-wave1a.md) 流程暂停新接收和领取，结清活动责任，验证备份并实际停止全部旧写入者。运行角色在 owner 作业期间保持 NOLOGIN。
2. 由同一数据库 owner 用现有 `check`、`apply`、`observe` 完成 0093→0094。最完整的连续前序链在 `--oldest-source` 和 `--oldest-intent` 之后追加 `--placement-source` 和 `--placement-intent`。只有观察到 `committed_current` 后才安装目标声明。历史 Worker、事件和资源账本保留。
3. 保持进程停机，向相同 release 和数据库连接配置注入私有容量源配置。运行 `python -m dlr.control.control_identity_init_v4 ha-configure <storage-domain> <epoch>`，初始化双节点容量权威。成功返回 `configured`，相同参数读回为 `verified`。若提交回执未知，在独占停机条件下以相同参数读回。
4. 恢复受限运行角色。在不同宿主各启动一份同摘要镜像，使用同一 Master Key、Admin/Worker Token、Worker 证明密钥和数据库运行角色。设置 `DLR_WORKER_V4_DEPLOYMENT_MODE=C` 与 `DLR_CONTROL_HA_SLOT=a` 或 `b`。同一槽的两个活跃实例不能并存，旧进程的身份不能重用。容量指纹不一致时只保留可以继续处理的管理及责任 API。

持久化身份只包含租约和校验材料。私有部署声明、TLS、认证材料和节点地址从部署环境提供。升级后以当前 schema 的制品向前恢复，保留已有责任和卷。

## LB 与能力

参考 [HAProxy 配置](../../deploy/control-ha/haproxy.cfg.example) 部署 Token/Worker 和账户入口。静态 Web 继续由现有 Web 入口提供，浏览器在同源 API 路径访问对应入口。替换示例证书、证书主机名和地址，Control 端口只接收受信内网流量。使用 `uvicorn --no-proxy-headers --ssl-certfile <cert> --ssl-keyfile <key>`，代理验证 Control 证书。Worker 凭据兑换依赖 Control 实际收到的 HTTPS。用 `DLR_CONTROL_HA_TRUSTED_PROXY_IPS` 配置直接连接 Control 的代理 IP。账户入口将公开 `/api/` 请求转到私有前缀，外部请求仍走原来的会话、CSRF、来源或 Worker 授权合同。

- `GET /api/health/live` 判断进程是否能应答。
- `GET /api/health/ready` 供负载均衡使用，以可写数据库、schema 和有效 Control 权威为基础。
- `GET /api/health` 保留聚合诊断，并单列当前节点、集群登记数及接收、发布、存储能力。Broker 中断时，依赖 Broker 的能力单独降级，合法续租、JSON 结果、Receipt 读回使用任意仍就绪的节点。

内部 Broker 采用三节点时，按 [RabbitMQ HA 部署与恢复](rabbitmq-ha.md) 配置 AMQPS、管理 HTTPS 和两个队列族的成员。

正在准备的 Claim 使用原 proposal 发起确认。收到请求的节点在必要时向预置对端转交一次，原节点继续校验 proposal、身份、期限和子进程预算。为每节点配置 `DLR_CONTROL_HA_PEER_URL` 及 `DLR_CONTROL_HA_PEER_CA_FILE`，将该地址指向相对槽位的专用私网 HTTPS 入口。TLS CA 应仅签发此内部用途的服务证书。普通负载均衡、私网转交都禁用非幂等请求自动重试。正在进行的 Assist 中断后由用户决定是否重试。Execution SSE 中断后浏览器通过已有 Execution ID 做有界读取。

这批的双 Control 接受纯 JSON 执行。Claim 提交和结果暂存各自确认本地子进程退出再返回证明。任一合格节点都可用同一持久 intent 和原证明完成结果提交，正文 MAC、有效期限和原 Attempt 围栏继续生效。接收新责任依赖本节点近 10 秒内的 Broker 校验，已知故障时关闭接收。旧 Receipt 读回、续租和可提交的结果分别判断。

## PostgreSQL 容量与维护

两节点使用相同的 `DLR_POSTGRES_CAPACITY_URL`、`DLR_POSTGRES_CAPACITY_TOKEN`、受信 CA 和接收配额，私有 CA 通过 `DLR_POSTGRES_CAPACITY_CA_FILE` 提供。端点为固定 HTTPS，返回 `schema_version=1`、`cluster_id`、`storage_domain_id`、正整数 `topology_epoch`、带时区的 `observed_at` 和非负整数 `writable_free_bytes`。采样覆盖实际可写主库和允许提升的副本容量域。DLR 按数据库时间核对 15 秒新鲜度与 5 秒未来偏差，容量下降至 5 GiB 暂停新接收，恢复至 6 GiB 后重新允许。不可用、过期或冲突的观测关闭新接收，旧 ID 仍按原授权读回。

两节点的应用预算合计最多 256 个 HTTP 在途、16 MiB HTTP 接收缓冲、128 MiB 来源缓冲和 16 条 intake 数据库连接。每节点最多 16 个 owner/诊断名额，并保留 4 条管理与恢复数据库连接。双节点启用来源服务时，每个节点为接管预留全部 16 席，在线连接测试会返回容量繁忙。PostgreSQL 最大连接数还需计入受监督子进程、监控、迁移和管理员连接。持久额度和速率由共享数据库事务裁决。

切换容量配置时依次停止新接收、关闭两节点的实际本地来源工作和在途请求，确认租约退出并停止 Control，隔离受限运行角色。由 owner 使用新配置和更大的 epoch 执行 `ha-configure`，保留已有额度占用，清除原观测并围栏旧实例。统一安装新配置后再恢复两节点。新容量源必须提供匹配的新 epoch 样本并达到恢复水位。实际数据库、冗余入口与独立共享存储按[模式 C 外部设施](control-ha-external-infrastructure.md)部署和验收。
