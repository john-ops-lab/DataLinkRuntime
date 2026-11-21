# 模式 C 外部数据库、负载均衡与共享存储

模式 C 组合双 Control、冗余入口、外部 PostgreSQL HA、[三节点 RabbitMQ](rabbitmq-ha.md)、[三类共享 POSIX 命名空间](control-ha-shared-storage.md)和多 Worker。基础安装与私有对端入口见[双 Control 基础部署](control-ha-foundation.md)。每个供应侧组件都要记录节点、数据盘、网络和仲裁的实际失效域，按能够隔离的故障验证恢复。

## PostgreSQL 的持久提交与提升

参考部署使用 PostgreSQL 16 物理流复制。每个可能提升的节点都列入当前主库的 `synchronous_standby_names`，如 `FIRST 2 (b, c)`。数字等于候选数。为运行角色、数据库和维护作业保留 `synchronous_commit=remote_apply`、`fsync=on` 和 `full_page_writes=on`。通过角色和实际连接核对生效设置。限制复制身份和网络来源，验证 TLS 主机名，并以固定的 `application_name` 识别每个候选。逐台核对系统标识、时间线、同步状态与 replay LSN。表空间和磁盘配额也要纳入供应方的容量声明。

失去要求的同步副本时，写事务可等待同步。运行连接继续开放时，不得通过取消 COMMIT 等待、减少要求的同步副本数或关闭同步来恢复写入。PostgreSQL 可能在受主动取消时已经本地持久化并发出告警，此时结果须视为未知。操作员需先围住整套旧写入口和存量连接。原主故障后，只能提升此前处于声明同步集合、有真实持久提交保障的副本。候选缺失、旧主无法隔离或同步证明未知时，保持写入口关闭。异步部署不能承诺已确认责任零丢失。[PostgreSQL 同步复制](https://www.postgresql.org/docs/16/warm-standby.html#SYNCHRONOUS-REPLICATION)

1. 记录故障时刻、原主和时间线。关闭两台代理的运行数据库入口，终止代理中的旧后端连接。停两台 Control，核对连接池、native 来源进程及在途请求已退出。对原主使用供方的节点隔离、断电或已验证的进程及其自动重启隔离，留存隔离回执。
2. 在维护通路核对同步候选和原主失权，由供方提升。选用与原主同属一个系统标识的新时间线，在新主仍隔离运行连接时绑定仍可用的同步候选，并验证实际 `sync_state=sync`、`state=streaming` 和 replay。旧节点沿新时间线重建或按供方流程追赶，恢复同步资格前不得重新成为写节点。
3. 保持运行角色 `NOLOGIN`，只通过维护通路保留一个应用库 owner 会话。确认没有其他应用库用户会话和 prepared transaction，等原 Control 及订阅租约自然结束。在同一逻辑数据库容量域使用更大的 epoch 运行 `python -m dlr.control.control_identity_init_v4 ha-configure <storage-domain> <epoch>`。以 `configured` 或同意图读回的 `verified` 为准。已接受记录和占用沿用原库。
4. 用新主和同步候选生成匹配新 epoch 的真实容量采样。恢复受限角色登录，在隔离的维护通路用实际运行角色和实际连接参数核对数据库身份及 `SHOW synchronous_commit`，随后断开该验证连接。两台代理开放唯一主库，再启动 Control。用合法新请求的提交与独立读回验证恢复。旧 ID、提交结果未知及原生重投按原键核对。文件节点具备安全挂载后，按共享文件指南对三个启用的命名空间运行新的双端 `prove`，确认新实例完成登记，逐个核对原冻结对象。

## PostgreSQL 容量 provider

[参考采集与 HTTPS 服务](../../deploy/control-ha/postgres_capacity.py)覆盖主库及全部可提升副本。在每台数据库节点的 PostgreSQL 进程及挂载命名空间内，用独立维护身份运行 `observe --activation-id <uuid> --database <runtime-db> --role <runtime-role>`。采集进程通过本机 Unix socket 连接维护库。本机 PostgreSQL 提供角色、复制状态及实例名 `dlr_capacity.node_id`。该设置来自各实例独立的私有配置，需分配唯一值并避免在基准备份时复制。容量直接读取实例 data 目录和 `pg_wal` 所在的文件系统。输出用于运维采集。服务仅向 DLR 返回 D10 的六个字段，容量取所有节点 data/WAL 余量的最小值。如果卷受配额限制，或使用其他表空间，须由供方计入额外限制后给出保守值。

- 采集账户只需要 PostgreSQL 维护库 `postgres` 的连接、复制观测、设置读取、系统标识读取及其本机目录的查询权限。用独立账户和本机 Unix socket。可向专用角色授予 `pg_monitor` 和 `EXECUTE ON FUNCTION pg_catalog.pg_control_system()`，并在目标业务库移除其 CONNECT 权限及 PUBLIC 默认授权。证书、数据库口令、私有观测和采集身份由部署方管理。可用独立受限 SSH 命令按节点拉取。
- 从[拓扑示例](../../deploy/control-ha/postgres-capacity-topology.json.example)创建私有拓扑文件。每 2–3 秒从该拓扑采集一整轮，使用同一个 `activation_id`、业务库和运行角色。将完整本轮输出组装为 `{"samples":[…]}`，在目标目录用临时文件加原子 rename 发布整批。丢失任一成员时旧批次会在 10 秒内失效。每次换代更新主库、全部候选、实际时间线、DLR epoch 和新生成的 `activation_id`，以前的采样无法用于新拓扑。时钟保持同步。DLR 以数据库时间再检查原有 15 秒窗口并持久记录观测。
- 先运行 `python3 postgres_capacity.py check "$TOPOLOGY" "$BATCH_FILE"`。通过后，使用 `serve "$TOPOLOGY" "$BATCH_FILE" --address "$PRIVATE_BIND" --port "$PORT" --token-file "$TOKEN_FILE" --certificate "$CERT" --private-key "$KEY"`。两台 Control 使用同一 HTTPS URL（`/capacity`）、受信 CA 和 Bearer token。服务缺少成员、实际同步条件或存在异步的业务库/角色配置时返回 503，DLR 也会拒绝超时、错误域或旧 epoch。对运行连接禁止通过 DSN 或会话声明覆盖同步设置。容量汇总和 HTTPS 服务部署在声明故障会保留的故障域，或提供稳定 URL 下连同采样权威的受控切换。可用容量沿用停止 5 GiB、恢复 6 GiB。

数据库部署至少预算双 Control 的 16 条 intake、8 条管理和恢复连接，再计复制连接、探针、独立运维、迁移和数据库紧急保留。记录每个节点的 `max_connections`、`max_wal_senders`、保留连接与实际峰值，照顾故障后单 Control 接管全部订阅的本地并发预算。

## 冗余入口

在两个独立 Linux 节点运行 [Control HAProxy](../../deploy/control-ha/haproxy.cfg.example)，用[私有 VRRP 示例](../../deploy/control-ha/keepalived.conf.example)提供稳定地址。在等效外部负载均衡上保留同样的路径、TLS 和不自动重发写请求的行为。VRRP 需要受控的同一 L2、兼容的地址接管与明确的分区路由。两台专用入口机均先启用 `net.ipv4.ip_nonlocal_bind=1`，保证尚未持有 VIP 的节点可以启动 HAProxy。检查两台代理的本机 Unix Runtime socket 和 `127.0.0.1:18080/ready`，再启动 VRRP。VRRP 探测代理的真实应答，活动代理卡死时也能转移。节点共损、L2 整体不可用和 VIP 回切均须按实际拓扑验证。

稳定数据库入口可附加[专用 TCP 后端](../../deploy/control-ha/postgres-rw.cfg.example)。示例只监听私有 VIP 并限制来源。允许两台 Control 的固定私有来源及经授权的运维探测入口。数据库为代理来源配置受限网络和运行角色权限。运行客户端使用 `sslmode=verify-full` 和稳定主机名，数据库服务端证书含该名称。两台代理默认关闭全部候选后端。只在供方验证旧主隔离和新主同步后端到位、容量权威切换成功后，动态启用两端同一个后端。对于 HAProxy 管理 socket，先对旧后端执行 `disable server postgres_primary/<name>` 和 `shutdown sessions server postgres_primary/<name>`，待新主完成验证后执行 `enable server postgres_primary/<name>`。两台代理分别在 global 配置仅限本机操作身份的 Unix Runtime socket，使用 `level admin` 和 `mode 0600`。对两台分别核对命令执行结果。重启代理后根据当前权威重新授权。TCP 健康检查用于摘除失联地址，提升由数据库供方执行。

## 共享文件存储

参考拓扑使用跨三个 Linux 节点的 GlusterFS 三副本卷，客户端仲裁要求多数，服务器仲裁同样要求多数。启用 `cluster.ensure-durability`，关闭异步写回，并验证实际副本卷、有效的服务端/客户端仲裁及持久化选项。[Gluster 仲裁](https://docs.gluster.org/en/latest/Administrator-Guide/arbiter-volumes-and-quorum/)

两台 Control 通过各自的原生客户端，以相同 UID 使用同一卷内三个独立目录。提供多个可用 volfile 入口。每个目录按[共享文件指南](control-ha-shared-storage.md)登记。逐命名空间验证新鲜可见、原子发布、持久化和跨客户端互斥。在只剩少数派时，禁止其继续成功写入或获得独占锁。多数派恢复用相同对象键、size 和 SHA-256 校验原件，再用新键验证跨节点发布。检查复制修复和锁恢复后再恢复失联节点的写资格。

单节点 NFS 可做文件语义预检。声明存储故障可恢复时，受影响的存储节点与测试客户端必须有可独立隔离的 Linux boot、挂载和真实持久卷。文件引用、数据库容量和网络拓扑的告警都要与实际恢复路径对应。

## 验收与灾难恢复

数据库的三个窗口分别保留原键和独立证据。HTTP 已返回 202、原生来源已确认、COMMIT 已送出且客户端未收到回复，各自从主库失效时刻计，到经原入口成功提交并独立读回，目标 60 秒。容量旧 epoch 在其时间仍新鲜时也要拒绝新接收，已有责任可读回。活动 LB 失效与存储成员失效或分区的有效新请求、跨节点写入和原件读回分别计时，目标同为 60 秒。首败、重试、拓扑及测试记录保留于验收台账。

记录数据库、存储、Broker、入口、仲裁和宿主的共同失效范围。VM 级验收覆盖可独立失效的来宾或服务节点。生产使用独立物理故障域时须重新演练。遇到多数派、可信旧主隔离或最新持久点丢失，保持接收关闭。保留原账本、共享目录、故障数据库和来源检查点，核对备份范围、冻结文件摘要及外部效果，再按[共享文件灾备步骤](control-ha-shared-storage.md#ai-附件与数据库恢复)和原身份合同恢复。记录备份的实际 RPO，逐 ID 对账后恢复业务。
