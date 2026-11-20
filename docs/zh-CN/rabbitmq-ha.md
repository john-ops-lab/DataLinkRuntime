# 内部 RabbitMQ 三节点部署与恢复

DLR 使用 [RabbitMQ 4.3.5](https://www.rabbitmq.com/docs/4.3/quorum-queues) 派发任务。工作队列和 Infrastructure DLQ 都需要三个同步的投票成员。三个 Broker 分别部署在可独立隔离的 Linux VM 或节点上。AMQPS 和管理 HTTPS 各使用稳定的冗余入口。业务订阅使用的外部 RabbitMQ、Kafka、RocketMQ 和 Kubernetes 仍有各自的可用性边界。

本阶段演练覆盖单台 Linux VM 或单个测试 Broker 失效。若两台 VM 共用物理宿主，整机停机时会同时失去两个投票成员，队列停止确认写入。需要承受任意一台物理宿主失效的部署应将三个投票成员放到三个独立的宿主故障域，并对该拓扑单独演练。

## 部署前记录

记录三个 Broker 的 Linux boot、VM 与物理宿主对应关系、稳定主机名、节点名、镜像摘要、持久卷和网络路径，以及每个入口背后的代理实例。为磁盘、内存、Erlang 分布端口、客户端连接和两个入口分别核对资源。在同一宿主共置 Control、Worker 或其他依赖时，记录停机共同影响。用隔离 vhost 演练，并记录测试前的队列参数、原业务键和 Outbox 责任。

[Broker Compose](../../deploy/rabbitmq-ha/compose.yml) 在每台 Linux 宿主运行一个节点。[Broker 配置](../../deploy/rabbitmq-ha/rabbitmq.conf.example) 中的地址与节点名要替换为私有网络的实际值。

- 为每个节点指定独立的 `DLR_RABBITMQ_DATA_DIR`、`DLR_RABBITMQ_CONFIG_FILE` 和 `DLR_RABBITMQ_TLS_DIR`。将 `DLR_RABBITMQ_NODE_HOST` 设为节点主机名，`DLR_RABBITMQ_NODE_NAME` 与 seed 列表中的 `rabbit@主机名` 对应。复用原短名节点时显式设置 `DLR_RABBITMQ_USE_LONGNAME=false`。数据目录归 Broker 运行身份所有，先放置内容相同、仅该身份可读的 `.erlang.cookie`。已经运行的节点以原节点名、命名方式和原数据目录加入方案。
- 安装各自的 `node.pem`、`node.key` 和 `ca.pem`。节点证书同时包含自身 DNS 名及稳定 AMQPS 名，节点私钥独立。将 Erlang 分布监听绑定到节点私网地址，限制 EPMD 和分布端口仅供 Broker 节点访问。用 `ss -ltn` 核对监听地址，用 `rabbitmq-diagnostics -s listeners` 核对协议。原生 AMQP 使用 TLS，管理 HTTP 和 Prometheus 指标端口限于本机回环。
- 参考配置将 Erlang 节点间的失联检测设为 20 秒，为 VM 突然失联后的入口恢复留出时间。将 `advanced.config.example` 随 Compose 一起分发到每台宿主的同一目录，或用 `DLR_RABBITMQ_ADVANCED_CONFIG_FILE` 指向 Broker 可读的私有配置。已有高级配置可在原 `kernel` 条目中合入该值。三个成员使用相同的 `net_ticktime`。现有集群改值时先停接收并排空，在允许停服的维护窗口停齐旧配置节点，再按既有集群的恢复顺序启动统一配置的节点。逐节点用 `rabbitmqctl eval 'net_kernel:get_net_ticktime().'` 核对，并在部署网络上复测丢包、抖动与恢复时限。[RabbitMQ 节点失联检测](https://www.rabbitmq.com/docs/4.3/nettick)
- 依次启动 A、B、C。全新部署的 A 用仅含自身的 seed 列表启动，B、C 使用参考配置的三个地址加入。以 `rabbitmqctl cluster_status` 核对相同集群和三个运行节点后，才配置新工作队列。扩容既有部署时，A 使用原数据目录和节点名，B、C 使用专用空目录及受控命名。若目标节点已有其他集群数据，先独立处置其责任。

在两个独立代理实例上安装 [HAProxy 配置](../../deploy/rabbitmq-ha/haproxy.cfg.example)，用站点提供的冗余入口指向这些实例。每台代理用实际地址和受信 CA 校验 Broker。AMQPS 在代理处透传 TLS，由客户端验证稳定名，建立后端连接失败时可换路。管理入口由代理终止 TLS，PEM 文件仅向代理运行身份开放，后端 HTTPS 校验节点名。运行入口允许只读 API GET。后端连接、空响应或指定 5xx 失败时，代理对 GET 执行有界换路。入队和 Confirm 仍由 RabbitMQ 与 Outbox 裁决。

两类入口的后端健康检查访问每个节点自己的管理 HTTPS，读取精简的 `/api/nodes`。在固定三节点集群中，代理只向看到至少两个运行成员的后端转发。AMQPS 探测还在该节点的 5671 端口验证实际 TLS 握手和证书。AMQPS 健康检查失败会关闭该后端的既有连接，使已连到少数派的 Worker 按原逻辑重连。该检查反映集群连通性，每条队列的状态与实际 Confirm 仍分别核验。代理从私有环境读取 `DLR_RABBITMQ_PROXY_HEALTH_BASIC`，其值是探测账号 `用户名:密码` 的 Base64 编码。

## 角色与 Control 配置

Broker 运维凭据只用于隔离管理路径或宿主 CLI。运行身份各有独立凭据和目标 vhost。通过受控终端输入密码，或使用私有文件经 stdin 供应。凭据保存在权限受控的部署配置里。

| 身份 | 权限 |
| --- | --- |
| Control AMQP | 仅指定 vhost 的 DLR 工作队列、派发交换机和 Infrastructure DLQ/DLX。允许现有声明、绑定、发布和基础设施 DLQ 消费。 |
| Worker | 仅 read 自己的 `dlr.worker.<ID>.q`，授信、队列和稳定 AMQPS 名按现有 Worker 私有配置安装。 |
| Control 管理探测 | 使用独立账号和 `management` tag。只绑定目标 vhost，configure/write/read 使用 `^$`。运行入口的功能是读取 feature flags 和队列状态。 |
| 代理多数派探测 | 独立账号和 `monitoring` tag，用于读取各节点的集群连通性。目标 vhost 的 configure/write/read 均使用 `^$`。 |

在运维节点确认变量均来自目标部署。下面的 Control 资源模式涵盖 DLR 当前的两种队列和两个交换机。

```sh
dlr_resources='^(dlr\.execution\.(dispatch\.v1|infrastructure\.(dlx|dlq))|dlr\.worker\.[1-9][0-9]*\.q)$'
rabbitmqctl set_permissions -p "$vhost" "$control_user" "$dlr_resources" "$dlr_resources" "$dlr_resources"
rabbitmqctl set_permissions -p "$vhost" "$management_user" '^$' '^$' '^$'
rabbitmqctl set_user_tags "$management_user" management
rabbitmqctl set_permissions -p "$vhost" "$proxy_probe_user" '^$' '^$' '^$'
rabbitmqctl set_user_tags "$proxy_probe_user" monitoring
rabbitmqctl set_permissions -p "$vhost" "$worker_user" '^$' '^$' "^dlr\.worker\.${worker_id}\.q$"
```

Control 使用 `DLR_RABBITMQ_URL` 指向 AMQPS，`DLR_RABBITMQ_MANAGEMENT_URL` 指向独立 HTTPS，`DLR_RABBITMQ_MANAGEMENT_USER` 和 `DLR_RABBITMQ_MANAGEMENT_PASSWORD` 来自私有配置。将 CA 纳入进程的信任链，例如显式配置 `SSL_CERT_FILE`。核验两个入口的 DNS 名与证书。以运行管理账号验证队列和功能探测成功，再验证该账号的 AMQP 发布、消费和拓扑变更均被拒绝。

初次开放 HA 前，用下一节的逐队列证据确认两个队列族均达到三名同步 voter，再启用 `DLR_RABBITMQ_HA_ENABLED=true`。双 Control 使用同一配置。启用此开关会改变 Control 容量配置指纹，按 [双 Control 容量维护流程](control-ha-foundation.md#postgresql-容量与维护) 先停接收并排空，以实际容量域和新 epoch 重绑配置，再恢复两端。单 Control 可在单独的部署维护窗口切换。确认新探测后再开放新业务。

## 原位增加或替换队列成员

先记录稳定 vhost、队列名、原成员和 leader、绑定及原消息/责任清单。准备经核验的可读备份。保留原队列、原持久卷、旧节点名和原绑定。集群中新加 Broker 不会自动把已有队列扩成三个副本。对每一条固定工作队列和 Infrastructure DLQ 分别操作。

```sh
rabbitmqctl list_queues --vhost "$vhost" name type leader members online arguments \
  messages_ready message_bytes_ready messages_unacknowledged
rabbitmqctl list_bindings --vhost "$vhost" source_name destination_name routing_key
rabbitmq-queues quorum_status --vhost "$vhost" "$queue"
rabbitmq-queues add_member --vhost "$vhost" "$queue" "$second_node"
rabbitmq-queues quorum_status --vhost "$vhost" "$queue"
# Confirm member synchronization and voter status before the next operation.
rabbitmq-queues add_member --vhost "$vhost" "$queue" "$third_node"
rabbitmq-queues quorum_status --vhost "$vhost" "$queue"
```

4.3.5 默认将新副本加入为 `promotable`。读取队列的 Raft 成员状态，确认已经同步并成为 voter 后再加入下一成员。中断时以现有成员及进度续办。核对原消息、绑定和 Outbox 责任，再验证原可靠策略和容量上限。工作队列维持 reject-publish、原生延迟重投及 at-least-once DLX，DLQ 维持原有 reject-publish 与容量合同。

替换节点时先接入健康的新宿主，逐队列验证新副本实际可投票，再逐队列从仍有多数的健康集合移除旧成员。该运维窗口停新接收，等待实际成员列表恢复三名并复核 leader、原消息和容量后开放。未证实多数派可用时，保留现有状态与责任并恢复原可用成员。

```sh
rabbitmq-queues delete_member --vhost "$vhost" "$queue" "$retiring_node"
rabbitmq-queues quorum_status --vhost "$vhost" "$queue"
```

## 观测与故障演练

`GET /api/health` 的 `rabbitmq.broker.quorum_queues` 分别展示 Worker 队列和 `infrastructure_dlq` 的成员数、报告在线数和 leader 是否在线。节点名称不进入应用健康响应。成员错配为 `incomplete`，无多数或 leader 为 `unavailable`，观测过期或格式无法确认时为 `unknown`。已确认三成员队列只剩两名在线且 leader 可用时显示 `degraded`。以 Broker CLI 核对实际投票和同步状态，以实际 Confirm 和 Claim 证明业务可用。

- 分开中断 AMQPS 活动入口、管理活动入口和 leader。轮流隔离每个 Broker 所在的单台 VM 或测试节点，恢复正常的三个投票成员后再进入下一轮。核对原 Worker 身份经稳定地址重新消费，发布经过原 Outbox，管理只读查询走存活路径。管理入口错误与单队列缺少 quorum 分开观察。分区前记录 Worker 实际连接的 Broker，确认少数派连接被摘除后重新消费。
- 按真实故障 T0 分别记录稳定 AMQP 入口获得首次有效 Confirm 和 Worker 获得首次有效 Claim。保留多数派时两项均须在 60 秒内成功。对 2:1 分区中的少数派直接观察发布结果，恢复后再核对原三成员与 leader。
- 确认回执丢失时保留原 Outbox 与输入键，核对原记录的后续回访和重发结果。以 Receipt、Execution、Attempt、物理清理和独立目标逐条对账。已确认责任丢失数和并存的双有效 Attempt 数均为零。记录首败、原始期限、重试与观察版本。
- 轮换时预配权限相同的新身份并验证路径，再更新私有配置，清理旧连接并撤销旧凭据。管理凭据轮换后从两端核对新探测，Worker 独立账号轮换按[原维护流程](worker-v4-dispatch-contract.md)操作。日志和演练记录保留稳定错误码、计数和时间点。

分区恢复时从确认可用的多数派与原数据库责任继续。用原 ID 读回已提交事实，分别确认入口恢复、后续新 Claim 和实际业务结算。
