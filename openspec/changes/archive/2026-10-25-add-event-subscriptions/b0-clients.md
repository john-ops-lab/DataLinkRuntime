# B0 客户端选型和 API 探针

核对日期：2026-10-23。本记录固定 B0 1.2 选型和可复跑证据；B2/B4 的持久恢复、B3/B5 的目标业务读回仍须另行验收。

## 最终选择与弃选

| 用途 | 精确版本 | 理由 | 许可 |
| --- | --- | --- | --- |
| Kafka Consumer/Admin | `kafka-python==3.0.11` | cluster/topic UUID、assign/seek、pause/resume、手动 commit、显式 commit/close timeout；真实失联关闭实验通过 | Apache-2.0；客户端无强制传递依赖 |
| Kubernetes API/config | `kubernetes==35.0.0` | 官方生成客户端，与 1.35 服务对应；复用 TLS/auth，使用有界原始传输处理 LIST/WATCH | Apache-2.0 |
| HTTP 传输快照 | `urllib3==2.8.0` | 与现有后端锁一致；有 `HTTPResponse.read1(amt, decode_content=False)` | MIT |

版本在 [requirements-clients.txt](../../../../tools/subscriptions/requirements-clients.txt) 固定；B0 不向生产后端添加运行依赖。Kubernetes 35.0.0 的实际要求是 `urllib3!=2.6.0,>=1.24.2`，没有 `<2.4` 约束。初始试装 Kubernetes 34.1.0 遗留 urllib3 2.3.0，发现后已升级并对最终 2.8.0 重验，不降低原后端依赖。

比较与首败：

- **弃选 `confluent-kafka==2.16.0 / librdkafka 2.16.0` 作为运行消费者**。安装、cluster/topic ID、assign/commit/pause 与普通关闭均成功；但真实消费组已消费后，将所有既有 TCP 连接置为黑洞，带 pending async commit 的 `Consumer.close()` 在 35 秒子进程 watchdog 到期前没有返回。阶段证据确认停在 close 本体；不是 join/accept 超时。3 秒 socket、6 秒 session、30 秒 max poll 参数不能替代公开关闭预算。首败保留，不能以普通关闭 27 毫秒替代故障结论。
- `aiokafka` 原生 asyncio 与手动 commit/rebalance listener 可行；当前合同选择专用 owner 线程，最终 Kafka Python API 的可观察关闭预算更合适，不再维护第二套异步消费生命周期。
- `kubernetes-asyncio` 可行，但本稿复用同步客户端的 TLS/auth 与专用线程管理。没有声称其他成熟方案无法实现合同。
- Kafka Python **Producer** 不接受 null header 值，Consumer 实际能无损读取。因此 [requirements-fixtures.txt](../../../../tools/subscriptions/requirements-fixtures.txt) 额外保留 Confluent **Producer** 仅生成测试数据；它不进入 Control 依赖，也不构成运行消费者双客户端方案。

## Python、平台和支持矩阵

实际安装导入通过：macOS arm64 / CPython **3.13.14**；项目 Control 镜像 Linux aarch64 / CPython **3.13.16**。主 Agent 另以完整后端冻结约束解析三项最终依赖，无降级或依赖替换冲突。Linux 镜像安装日志、发行物 SHA-256、完整 freeze 保留在私有证据中。

| 发行物 | wheel | 验证边界 |
| --- | --- | --- |
| Kafka Python 3.0.11 | `kafka_python-3.0.11-py3-none-any.whl` | macOS arm64、Linux aarch64 真实安装；Python ≥3.8 |
| Kubernetes 35.0.0 | `kubernetes-35.0.0-py2.py3-none-any.whl` | 上述平台真实安装；纯 Python 本体不代表其全部依赖支持任意平台 |
| urllib3 2.8.0 | `urllib3-2.8.0-py3-none-any.whl` | Python ≥3.10，与原后端锁一致；上述平台真实安装 |

不把 wheel 的 any 标签当成全平台运行验收。Windows、musl、自由线程 Python、其他 CPU 架构未验收。

| 来源 | B0 实测目标 | 本批通过 | 以后仍须验证 |
| --- | --- | --- | --- |
| Apache Kafka | 官方 JVM `apache/kafka:4.1.2`，单节点 KRaft | 非空 cluster ID、非零 topic UUID、显式 assign/seek、组分配回调、下一位置 commit、pause/resume、bytes/null/tombstone/重复与 null header、关闭不自动确认、既有 TCP 黑洞关闭 | K03–K07 的 rebalance/crash/retention/topic 重建、事务提交/中止可见性、TLS+SASL/SCRAM 握手和轮换 |
| Kubernetes | `rancher/k3s:v1.35.9-k3s1` 的真实 API server，无工作负载节点 | Pod/Deployment 各 3 页 LIST、同 collection RV、WATCH 修改、idle stop、有界 raw LIST/WATCH、JSON 解码前拒绝超限页/行 | RBAC 拒绝、分页失效/410、UID 重建、selector 离开、批次与目标保护等 U01–U10 |

Kafka 初期要求可获取 cluster ID 与 topic UUID；身份缺失、权限不够或不支持时阻塞，不自动降级为仅按 topic 名去重。topic UUID 能识别同名重建，不代表所有重建竞争窗口已经验收。K8s `cluster_id` 使用显式逻辑身份和 TLS 信任，不伪造客户端可读的全局集群 ID。

隔离探针使用 Kafka PLAINTEXT；产品生产候选仍是 TLS+SASL/SCRAM，未通过 K07 前不得展示为已支持。`read_committed` 配置成功不等于已验收事务隔离。

## 可复跑工具与实际结果

从仓库根运行；变量指向调用方私有目录/连接配置，不提交真实地址或认证值：

```sh
uv venv --python 3.13 "$DLR_B0_VENV"
uv pip install --python "$DLR_B0_VENV/bin/python" -r tools/subscriptions/requirements-fixtures.txt
"$DLR_B0_VENV/bin/python" tools/subscriptions/probe_clients.py installed
"$DLR_B0_VENV/bin/python" tools/subscriptions/probe_clients.py kafka --config "$DLR_B0_KAFKA_CONFIG"
"$DLR_B0_VENV/bin/python" tools/subscriptions/probe_clients.py kafka-resources --config "$DLR_B0_KAFKA_CONFIG"
"$DLR_B0_VENV/bin/python" tools/subscriptions/probe_kafka_close.py --config "$DLR_B0_KAFKA_CONFIG"
"$DLR_B0_VENV/bin/python" tools/subscriptions/probe_clients.py kubernetes --kubeconfig "$DLR_B0_KUBECONFIG"
# 重现被弃选消费者：预期 status=failed / TimeoutExpired，私有目录保留已到达的阶段。
"$DLR_B0_VENV/bin/python" tools/subscriptions/probe_kafka_close.py --client rejected --config "$DLR_B0_KAFKA_CONFIG"
```

Kafka 私有 JSON 使用 `bootstrap_servers` 参数（兼容旧探针的 `bootstrap.servers`），B0 fixture 仅支持 PLAINTEXT。工具随机创建 `dlr-b0-*` topic/group/namespace，仅删除自己创建的资源。K8s 使用待调度 Pod 与零副本 Deployment，无需拉镜像；不验证工作负载执行。异常输出只保留类型，避免外部异常暴露连接信息。

| 实验 | 最终结果 |
| --- | --- |
| Kafka 两分区普通 API/内容探针 | 通过；包括 null header、明确 assign、手动安全 next commit、pause/resume、close 不提交最后一项 |
| Kafka 普通 close | `0.017s`（仅这一次观测） |
| Kafka 真实消费后所有既有 TCP 黑洞，pending async commit 后 close | 最终复跑见私有 JSON，约 `3.01s`；close 显式 `timeout_ms=3000`，本故障通过上限 `5s`，子进程含 join/setup watchdog `35s` |
| Kafka 16 个 idle metadata Consumer | RSS 增量 `4256 KiB`；native threads `2→18→2`，fd `8→72→8`（含测量用 Admin 的稳定基线）；不是饱和消息负载或硬 RSS 证明 |
| K8s Pod / Deployment | 各 3 页/3 对象，同 collection RV，WATCH 修改通过；idle stop 约 `1.80s` |
| K8s 有界原始传输 | 4 KiB 分块，实际 16 KiB annotation 在降低至 8 KiB 的 LIST 整页与 WATCH 单行额度前拒绝，JSON 尚未解码；连接 finally 关闭/释放 |

初次候选/API 适配失败（topic UUID 实际是字符串、Admin group 方法名差异、仅 internal topic 时 topics() 为空）、初始预热基线失败，以及全部首败与最终结果分开保存；不将工具适配错误说成来源故障。Ruff 检查通过。每条 `passed` 仅对应表中范围，未创建 Inbox/Execution，未验证业务目标。

## B1/B2/B4 必须遵循的实现边界

1. **单一 owner 线程**：KafkaConsumer 不是线程安全资源。一个实例全部 API 固定同一 owner 线程，SDK 自带 I/O 线程另计资源；不能把同一实例任意提交给池中不同线程。平台 rebalance/commit callback 仅做有界内部动作，捕获异常后转 blocked/fence，不能向库传播异常：close 开始先置 `_closed`，callback 抛出可能使后续 client.close 未执行且第二次 close 成为 no-op。
2. **分开超时**：poll `100–200ms`；bootstrap `3000ms`；request `10000ms`；session `6000ms`、heartbeat `2000ms`、max poll `30000ms`；commit 与 close 显式 `timeout_ms=3000`。close timeout 约束协调器等待，不把它误写为包括 DNS/自定义回调在内的单一墙钟保证。固定部署的 glibc 2.41、NSS files+dns、单 resolver、无 search 下，普通 DNS 全黑洞名义约 10 秒/lookup；此环境前提和具体客户端操作应分别计预算，运行在其他 resolver/NSS 配置前重新验证。
3. **先 fence 再回收**：停用/失租先撤销数据库 generation；客户端实际关闭返回前继续占用监听 permit，绝不能在超时后另开补位线程使总量无界。黑洞实验的子进程 kill 只是防探针挂住，不是生产取消线程的实现。成功回收只是已接管责任之外的连接生命周期，不能改变 T1/外部 ACK 合同。
4. **原生/协议内存另算**：fetch 与每分区 fetch `1 MiB`、接收 frame `2 MiB`、每 poll 最多 16 项。首个大 record batch、压缩展开和对象副本的成本不能只按 fetch 参数相加当成 RSS 上界；必须接最终单项检查与应用总预算，并在 B3 负载复测。16 个空闲单 broker Consumer 的数据不扩展成多 broker 饱和负载结论。
5. **K8s 不直接使用 Watch.stream 接管**：它在换行前可无限累积 bytearray，且 generated REST 的 ApiException 可能隐式读取整个错误 body。B4 必须复用 ApiClient 的认证与 TLS pool，`pool_manager.request(preload_content=False)` 后 `read1(amt=4096, decode_content=False)`，对非 2xx 同样受限；发送 Accept-Encoding:identity 并拒绝其他压缩编码，分页/行在 JSON 解码前检查累计额度，同时使用 socket read timeout、总 deadline 和停止信号。探针证明该底层接口真实可用；生产逐事件释放内存，不能把整个 WATCH 收成列表。
6. **K8s 关闭**：Watch.stop 仅设协作标志，不中断阻塞读。探针 idle 测量用 server timeout 2 秒、request connect/read 3/4 秒；raw 读取使用 connect/read 各 3 秒与总 deadline。退出必须 close/release 当前 HTTPResponse，ApiClient.close 只回收 ThreadPool，还须 clear REST pool。长连接周期可续接，持久 RV 必须来自已提交责任，不使用库内部自动前推位置。

## 官方依据

- [Kafka Python 3.0.11 发行物](https://pypi.org/project/kafka-python/3.0.11/#files)、[Consumer API](https://kafka-python.readthedocs.io/en/master/apidoc/KafkaConsumer.html)；精确已安装源码的 close/commit Timer 另由独立审查核对。
- [Kubernetes 35.0.0 README](https://github.com/kubernetes-client/python/blob/v35.0.0/README.md)、[Watch 源码](https://github.com/kubernetes-client/python/blob/v35.0.0/kubernetes/watch/watch.py)、[发行物](https://pypi.org/project/kubernetes/35.0.0/#files)。
- [urllib3 2.8.0 发行物](https://pypi.org/project/urllib3/2.8.0/#files)；read1 的 decode_content=False 路线已在该真实安装版本复跑。
- [Confluent Consumer C 包装源码](https://github.com/confluentinc/confluent-kafka-python/blob/v2.16.0/src/confluent_kafka/src/Consumer.c)：close 无公开 timeout；其轮询成功与黑洞失败均保留。
- [Apache Kafka 4.1 Docker](https://kafka.apache.org/41/getting-started/docker/)、[K3s 1.35 发行说明](https://docs.k3s.io/release-notes/v1.35.X)。
