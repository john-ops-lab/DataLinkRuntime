# 远程固定 Worker（W1-A 至 W1-I）

本阶段使用一个 Control 应用进程，默认一个固定 Worker；W1-E 显式配置后允许两个固定 Worker 各自单并发执行。默认支持标准库 Python，W1-G 可显式启用无第三方依赖的 JavaScript；触发仍限普通手动/定时任务。输入默认限 none/JSON，W1-I 可显式启用下方受限托管文件。W1-H 的业务凭据许可保持；I 只增加精确 H→I 停机发布对，历史 F/G/H 写操作仍须使用对应原二进制。各阶段真实拓扑、版本、审查和 CI 以相应 PR 为准。正式合同仍位于原 [remote-fixed-python-worker](../../openspec/specs/remote-fixed-python-worker/spec.md) 路径。

不支持第三方依赖、任意 URL/宿主路径/共享卷输入、内置包、未许可业务凭据、TypeScript/Java/Go、Webhook/事件/批次、旧身份迁移、文件任务的历史恢复、异常后的自动恢复、Pool 或 HA。不能把业务凭据写入代码来绕过限制。#129 在阶段 PR 后保持开放。

W1-B 新检查点与管理员显式恢复的增量合同、旧残留限制及宿主操作流程见 [Wave 1-B](remote-worker-wave1b.md)。完整 stopped 后未提交结果的受控结清见 [Wave 1-C](remote-worker-wave1c.md)。以下故障阻断记录是 W1-A 的历史边界，不能当作 W1-B 恢复通过证据。

W1-D 在现有系统状态页增加受限 v4 观察和标准排空/激活操作；具体接口、提交未知及升级边界见下方“受限 v4 运维界面”。它不增加恢复向导、强制停止、凭据管理或 HA。

## 候选代码与依赖

| 入口/职责 | 保留的实现 | 理由 |
| --- | --- | --- |
| Control 应用 | `control/app.py` 中显式开启的 identity/enrollment/instance/activation/attempt/claim/result/cleanup v4 API | 只有此处实际挂载的业务入口属于阶段运行路径 |
| 独立 Worker | `worker/agent_v4.py` → `consumer_v4.py` | 完成可信启动、真实 preflight、实例心跳和逐条 Broker 投递；配置无 Broker 时仅对账，不代表可执行 |
| 有预算领取 | `claim_preparation_v4.py`、`claim_http*_v4.py`、`claim_task_child_v4.py`、Control `claim_{coordinator,child,envelope}_v4.py` | 解析准备与最终 Claim 分开；未知提交与明确容量等待不得混淆 |
| 执行 | `execution_supervision_v4.py`、`attempt_session_v4.py`、`attempt_supervisor_v4.py`及`attempt_supervisor_child_v4.py` | 监管子进程由父进程按文件路径启动；Python/JavaScript 复用既有 harness/Sandbox |
| 语言资格 | `common/worker_languages_v4.py`、`worker/node_runtime_v4.py` | 默认 Python；显式双语言须通过固定 Node 22 和真实标准 Sandbox/harness 探针及清理 |
| 文件输入 | `common/managed_files_v4.py`、Control `worker_input_v4.py`/`input_child_v4.py`、Worker `managed_files_v4.py` | 默认 disabled；复用原 Claim/Result 槽与 staging child，当前双证明准入，顺序有界下载后发布完整只读集合 |
| 本机责任 | `journal*_v4.py`、`cgroup_intent_v4.py`、`workspace_{fs,live}_v4.py`、`sandbox.py`、`cgroup_namespace.py` | 保持原根目录句柄、持久意图、物理执行归属与实际资源账目 |
| 结果/清理 | `result_build_child_v4.py`、`result_exchange_v4.py`、`cleanup_execution_v4.py`及Control对应intent/finalize服务 | 结果确认、物理清理、服务端结算及本地释放完成后再 ACK |
| 身份与证明 | enrollment/runtime credential/trust/lease session、proof wire、Control candidate/intent/authority/token fence | 对当前已启用操作验证身份、期限与提交归属；Control 身份记录不表示支持双 Control |
| 原有机制 | Outbox、Execution/Attempt/Slot、有限重试和现有 Python harness | 复用既有事务/执行合同，只保留 v4 所需差异；v3 默认行为单独保留 |

候选新增运行模块已按应用、Agent、初始化/凭据操作命令的静态引用及实际子进程启动点核对。公共协议对未来字段的校验属于直接依赖，不能把“可解析”当作已支持文件或业务凭据。

## 候选迁移链

基线为 `0054_issue222_receipt_ttl_guard`，保留编号和串行依赖；编号间隙不表示缺失需要补入的 Pool/HA 迁移。

| 迁移 | 阶段依赖 |
| --- | --- |
| 0055 | Worker identity、独立凭据及管理员/容量状态 |
| 0058 | Attempt/Execution 的 protocol 4 归属与冻结字段 |
| 0059 | 历史实例记录，防止旧实例 ID 重用 |
| 0063 | 既有候选链中的关闭状态兼容窗口表；不开放迁移窗口或其原型服务 |
| 0064 | 管理员维护操作审计，激活及后续排空复用 |
| 0065 | 候选链中的自报能力观察模型；不据此授予 ready 或构成服务端资源预留 |
| 0066 | `attempt.py` 使用的失联责任恢复等待字段；不授权自动物理回收 |
| 0069 | 管理员新身份 reservation 与 v3 同名接管拒绝 |
| 0072 | 单 Control 的声明、实例及签名权威前提；不启用 HA |
| 0080 / 0081 | 实例及 Attempt 的提交候选/围栏 |
| 0082 / 0085 | 结果和清理 intent、提交归属、清理结算标记 |

0063/0065 保留现有候选依赖链与模型一致性，不为它们增加窗口/容量产品接口或外围测试。迁移整体仍须通过新建、基线升级和旧责任保持的直接回归。

## 实验的边界

原多波次工作区的 `proof_probe_client_v4.py`、probe路由、`python_runner_v4.py`、`python_completion_v4.py`、`python_output_probe_v4.py`和`controlled_lifecycle_probe_v4.py`依赖隔离测试前提，不进入阶段运行入口。`workspace_recovery_v4.py`、`claim_journal_v4.py`、dependency broker/staging、历史回执和文件分段实验继续延期。

Pool/预留账本、来源接管、共享存储、容量provider和AI转换进程树实验保留为后续工作，不复制进本候选。已有测试只挑选直接验证实际调用链的用例；私有 runner、临时容器配置及原始证据不提交。

## 启用与责任边界

远程模式默认关闭。开启前必须识别旧 Worker、旧执行和不明归属，拒绝原地切换；旧部署继续使用默认 v3 模式。Control角色/信任配置、单进程限制与 Worker 持久锚点、实际有限资源须同时满足，单次数据库检查不能证明旧进程已停止。

正常完成后可排空、停止、以同一身份重启并显式恢复调度。有异常遗留时只保留、阻断并诊断，不清 journal、不换空卷、不改库解锁。物理停止、数据库终态、清理结算及 Broker ACK 是分别核对的事实。

以下命令使用候选的 standalone 配置。配置渲染、数据库状态、物理清理及 Broker ACK 分别核对；实际验收结果绑定阶段 PR 的代码和镜像。

## 专用部署准备

Control、PostgreSQL 和 RabbitMQ 放在服务端宿主，Worker 放在另一台 Linux 宿主。两端使用相同阶段版本；镜像固定 digest，Control 只运行一个应用进程。第二个有效 Control 会被启动门禁拒绝。默认关闭远程开关的既有部署继续使用 v3，不能通过打开开关迁移旧身份。

Worker 宿主需要统一 cgroup v2、Docker `cgroupfs` driver 和可委派的 `cpu`、`memory`、`pids` 控制器。复用现有宿主准备脚本，使用专用 unit：

```sh
sudo bash scripts/prepare-sandbox-host.sh \
  --unit dlr-wave1a-worker.service --cpu-quota 300% --memory-max 3G

sudo python3 scripts/prepare-worker-journal-anchor.py --create-fresh \
  --anchor /ABSOLUTE/PERSISTENT/PARENT/journal
```

父目录必须预先存在。`--create-fresh` 仅用于已确认没有旧责任的新目录；已有锚点使用 `--verify-existing`，不会更改权限或清理内容。宿主重启后重新准备 transient unit，再核对脚本输出的两个 cgroup 参数。不要修改其他部署的 Docker driver 来凑本阶段环境。

预建一个专属持久 runtime volume，并在整个正常停启过程中保持同一名称：

```sh
docker volume create dlr-wave1a-worker-runtime
```

将 [Worker 环境模板](../../deploy/standalone-worker/worker-compose.env.example) 复制到仓库外的私有文件。配置使用 external runtime volume 和预建 journal bind，缺失时拒绝启动；Worker 不挂载数据库、Control 业务目录或 Docker socket，不发布业务入站端口。参考配置的整个 Worker 进程树限制为 2.5 CPU、2.5 GiB、512 PID、禁止 swap；任务上限另为 1 CPU、512 MiB、128 PID，并为领取接收和 Agent 留出预算。调整时必须同时核对 Control 冻结的 profile、宿主 envelope 和实际预检，不能只增加 slots。

## Control 身份、数据库角色与 TLS

数据库先以迁移所有者执行候选的 `alembic upgrade head`，再准备独立运行角色。下面的角色和数据库名均为可替换的部署示例；密码通过私有配置或管理员交互设置，不写入仓库或日志：

```sql
CREATE ROLE dlr_wave1a_runtime LOGIN;
REVOKE CONNECT, TEMPORARY ON DATABASE dlr_wave1a FROM PUBLIC;
GRANT CONNECT ON DATABASE dlr_wave1a TO dlr_wave1a_runtime;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO dlr_wave1a_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO dlr_wave1a_runtime;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO dlr_wave1a_runtime;
REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM dlr_wave1a_runtime;
REVOKE DELETE ON worker_lease_candidates_v4, attempt_lease_candidates_v4,
  attempt_result_intents_v4, worker_cleanup_intents_v4,
  worker_maintenance_operations FROM dlr_wave1a_runtime;
-- W1-F 及以后：新建部署的批量 GRANT 也不能暴露 owner 专属回执。
REVOKE ALL ON control_image_transitions FROM dlr_wave1a_runtime;
```

运行角色不能是数据库、schema、表或受保护函数的所有者，不能继承管理员权限、创建 schema 或改变触发器。迁移账号仅供部署操作使用；`DLR_WORKER_V4_DATABASE_ADMIN_ROLES` 明确列出可信管理员角色，初始化完毕后退出管理员数据库连接。签发和运行围栏会拒绝其他可连接的旧登录角色、旧会话及 prepared transaction；不能通过缩小查询或放宽权限绕过。

Control 私有目录预置 `control.json`、`proof-key`、`tls-cert.pem`、`tls-key.pem` 和 `ca.pem`。参考镜像以 UID 0 运行，因此这些文件应归 UID 0 所有、权限 0600，目录 0700；自定义镜像须与实际运行 UID 对齐。`proof-key` 是 Ed25519 的 32 字节私钥种子，以 64 位小写 hex 保存。`control.json` 为以下闭合格式：

```json
{
  "cluster_id": "REPLACE_WITH_NONZERO_UUID",
  "authority_epoch": "REPLACE_WITH_NONZERO_UUID",
  "image_digest": "sha256:REPLACE_WITH_CONTROL_IMAGE_DIGEST",
  "managed_fingerprint": "REPLACE_WITH_64_LOWERCASE_HEX",
  "builtin_fingerprint": "REPLACE_WITH_64_LOWERCASE_HEX",
  "attachment_fingerprint": "REPLACE_WITH_64_LOWERCASE_HEX",
  "capacity_fingerprint": "REPLACE_WITH_64_LOWERCASE_HEX",
  "worker_proof_public_key": "REPLACE_WITH_32_BYTE_ED25519_PUBLIC_KEY_HEX"
}
```

四个指纹分别对应已冻结的存储命名空间和容量配置，使用其规范化声明的 SHA-256；它们只作声明一致性比较，不能证明共享存储或物理容量。集群、epoch、公钥、密钥和声明需持久保留，不能在每次启动时重新生成。默认初始化只创建首次权威或核对完全一致的已有权威；受控换镜像使用下方 W1-F 显式维护命令，不通过 init 覆盖。

Control 和 Broker 使用部署 PKI 签发的服务器证书，SAN 必须匹配实际连接的主机名或 IP；CA/叶证书提供完整链及正确的 Basic Constraints、Key Usage、SKI/AKI。`ca.pem` 是相应信任包；需要访问其他 HTTPS 目标时包含其授权信任链。客户端始终验证链和主机名。

将 [Control 运行环境模板](../../deploy/standalone-worker/control-runtime.env.example) 复制到仓库外，填入运行角色的数据库 URL、Control 专用 Broker 管理账号、独立 admin/legacy namespace Token 和 Master Key。另建只用于部署的 owner 环境文件，数据库 URL 指向迁移所有者，其他权威配置保持一致。以匹配镜像和私有目录执行：

```sh
docker run --rm --env-file /ABSOLUTE/PRIVATE/owner.env \
  --mount type=bind,src=/ABSOLUTE/PRIVATE/control,dst=/run/dlr-private,readonly \
  CONTROL_IMAGE_DIGEST alembic upgrade head

docker run --rm --env-file /ABSOLUTE/PRIVATE/owner.env \
  --mount type=bind,src=/ABSOLUTE/PRIVATE/control,dst=/run/dlr-private,readonly \
  CONTROL_IMAGE_DIGEST python -m dlr.control.control_identity_init_v4
```

两个命令都需要到专用数据库的网络连接；按部署网络补充 `--network`。owner 环境文件还需声明私有目录内的 `DLR_CONTROL_IDENTITY_FILE` 和 `DLR_WORKER_PROOF_SIGNING_KEY_FILE`。初始化返回 `initialized` 或 `verified` 才继续，`unavailable_or_commit_unknown` 保留结果未知。

启动 [Control standalone 配置](../../deploy/standalone-worker/compose.wave1a-control.yml) 前，在私有 Compose 环境文件中填写 `DLR_CONTROL_IMAGE`、`DLR_CONTROL_RUNTIME_ENV_FILE`、`DLR_CONTROL_PRIVATE_DIRECTORY`、允许 Worker 到达的 `DLR_CONTROL_BIND_ADDRESS` 和固定 `DLR_CONTROL_TLS_PORT`。配置直接在 Control 终止 TLS，默认仅绑定 loopback；实际远程部署显式选择服务端接口。外部 PG/Broker 地址及容器网络由部署方提供。

```sh
docker compose -p dlr-wave1a-control --env-file /ABSOLUTE/PRIVATE/control-compose.env \
  -f deploy/standalone-worker/compose.wave1a-control.yml config --quiet
docker compose -p dlr-wave1a-control --env-file /ABSOLUTE/PRIVATE/control-compose.env \
  -f deploy/standalone-worker/compose.wave1a-control.yml up -d
```

参考 Control 配置限制整个父子进程树为 2 CPU、1.5 GiB、256 PID、禁止 swap，默认 Claim/结果子进程地址空间为 512 MiB。领取和结果还会核对真实 cgroup；配置渲染成功不代表这些硬限已经生效。

## 新 Worker 引导

管理员通过 `POST /api/workers/v4/identities` 发送 `name` 和非零 UUID `request_id`，保存返回的数字 ID 和稳定身份 UUID；同一请求 ID 可幂等读回。随后通过 `POST /api/workers/{id}/credentials/enrollment` 获取一次性 enrollment。响应包含凭据，只保存到私有文件，不能出现在普通终端、日志或 PR。

Worker 私有目录归实际 Worker UID 所有，权限 0700，以下文件为单链接的 0600 普通文件：

- `enrollment.secret`：一次性 enrollment 值，由部署方安全分发。
- `trust.json`：`schema_version=1`，以及与 Control 声明相同的 `cluster_id`、`authority_epoch`、`worker_proof_public_key`。通过可信部署渠道分发，不从一次网络响应自动信任新公钥。
- `ca.pem`：Control/Broker 所需的完整信任包。
- `broker.json`：下列独立 AMQP 配置，`ca_file` 使用容器内路径。

```json
{
  "schema_version": 1,
  "worker_id": 123,
  "worker_identity_id": "REPLACE_WITH_RESERVED_UUID",
  "host": "broker.example.invalid",
  "port": 5671,
  "vhost": "/",
  "username": "REPLACE_WITH_WORKER_ACCOUNT",
  "password": "REPLACE_WITH_PRIVATE_PASSWORD",
  "ca_file": "/var/lib/dlr/private/ca.pem"
}
```

Broker 账户只允许读取该数字 ID 的 `dlr.worker.123.q`，configure/write 均为 `^$`，read 为 `^dlr[.]worker[.]123[.]q$`；队列、绑定和 DLQ 由 Control/部署账号创建。Worker 不使用管理接口账号。实际连接后还需验证其他队列、DLQ、发布和修改拓扑均被拒绝。

使用同一私有环境文件和 Compose project 先执行一次引导命令，替换下面的非敏感地址和身份占位值：

```sh
docker compose -p dlr-wave1a-worker --env-file /ABSOLUTE/PRIVATE/worker.env \
  -f deploy/standalone-worker/compose.wave1a.yml run --rm --no-deps worker \
  python -m dlr.worker.enrollment_v4_cli \
  --base-url https://control.example.invalid:8443 \
  --worker-id 123 --worker-identity-id REPLACE_WITH_RESERVED_UUID \
  --credential-directory /var/lib/dlr/private

docker compose -p dlr-wave1a-worker --env-file /ABSOLUTE/PRIVATE/worker.env \
  -f deploy/standalone-worker/compose.wave1a.yml up -d
```

引导成功后由命令持久保存 `runtime-credential.json`。交换未知时保留本地标记，停止自动重试，由管理员核对服务端状态后处置。已 enrollment 的身份通过管理员 `credentials/recover` 撤销旧运行凭据，再使用 `recover_credential_v4_cli` 安装绑定该身份的新响应；这不清除执行责任，也不提供异常历史恢复。尚未 enrollment 的身份由管理员重发 enrollment，并在受控暂存目录完成新交换后按同一安装流程分发。原 journal、runtime 和未知记录保持可追溯。

Agent 通过真实本地隔离预检、注册及 ready 后，管理员读取 `GET /api/workers/{id}/v4/status`，再向 `POST /api/workers/{id}/v4/activate` 发送新 `request_id` 和当前 `expected_version`。新身份保持 disabled，显式激活才领取任务。心跳、ready 和正常重启不解除管理员维护状态。

## 运行、排空和正常重启

任务保存标准库 Python 版本并绑定该 Worker，使用 none/JSON 输入。手动 `POST /api/adapters/{id}/executions` 和已启用定时任务共用接收门禁；语言、依赖、文件、内置包、未许可凭据或非阶段触发方式不支持时，在接受责任前返回 `worker_task_capability_unavailable`。旧快照仍在最终 Claim 复核，保留原责任并给出等待原因。

新部署可能为新建 Adapter 预填演示凭据绑定。无业务凭据任务应在接受执行前将其绑定集设为空；`PUT /api/adapters/{id}/credential-bindings` 的请求体为 `{"bindings":[]}`，不会删除平台凭据。门禁不会静默忽略已配置的绑定。

正常排空按以下顺序进行：

1. 保存状态 API 返回的 `maintenance_version`，向 `POST /api/workers/{id}/v4/drain` 发送新 UUID `request_id` 和该 `expected_version`。新 Claim 被拒绝，已有执行仍可完成结果和清理。
2. 保留原请求体；重复同一排空请求核对完成情况。仅当前维护版本仍等于原排空操作的 result_version 时，才可在责任归零后确认 disabled，不重复递增维护版本。后续 activate/reconcile 已推进版本时，旧 drain 返回 409，不能再用于确认新周期；恢复后的确认规则见下方。
3. 独立确认任务输出和终态日志、`cleanup_settled_at`、Slot、本地 workspace/journal、进程/cgroup 和预算归还，并确认 Broker 累计 ACK 增长、ready/unacked 归零。数据库 disabled 不是本地清理或 ACK 证明。
4. 使用同一 project、runtime volume、journal 锚点和凭据目录正常停止，核对 Agent 返回 `stopped`、容器退出 0。离线不会缩短原实例租约；停止后等待旧租约届满（最多一个 30 秒租约期），再创建新实例。
5. 用同一配置 `up -d`，等待新实例完成预检和 ready；管理员按最新维护版本显式 activate，再运行下一项任务。

```sh
docker compose -p dlr-wave1a-worker --env-file /ABSOLUTE/PRIVATE/worker.env \
  -f deploy/standalone-worker/compose.wave1a.yml stop --timeout 30 worker
docker compose -p dlr-wave1a-worker --env-file /ABSOLUTE/PRIVATE/worker.env \
  -f deploy/standalone-worker/compose.wave1a.yml ps -a
docker compose -p dlr-wave1a-worker --env-file /ABSOLUTE/PRIVATE/worker.env \
  -f deploy/standalone-worker/compose.wave1a.yml up -d
```

## 受限 v4 运维界面（W1-D）

管理员打开“系统设置 → 系统状态”，可查看“远程固定 Python（受限）”节点。原 v3 执行就绪和缓存入口不变；v4 不使用 v3 Broker 标志判断协议兼容，也不显示尚未支持的缓存管理。

v4 分别展示观测在线、调度状态、容量、活动 Attempt、平台未结责任及历史阻塞。正常运行中的责任不等于历史恢复阻塞；“状态观测正常”只表示这一份观察满足显示条件，不授予 Claim 权限，也不承诺下一项任务可执行。服务端 Claim 仍重新检查当前身份、证明、期限、资源和冻结任务。

观察复用共享 Worker 刷新：W1-E 支持最多两个 v4 节点，同周期逐个 GET，全局最多一个管理观察 GET 在途，分别保持 5 秒超时及 30 秒新鲜度；一节点错误只使该节点未知。目录超过两节点或不可信时显示范围限制并禁用新维护写入。普通用户不请求管理员明细。页面可见时沿用 30 秒周期，聚焦、手动刷新和操作后刷新共用单飞请求；隐藏页面不新增维护轮询。30 秒未成功刷新、隐藏恢复、缺字段或读取失败时，旧观察只作为带时间的历史记录，操作不可据此放行。每次打开确认及最终提交前都重新读取，身份、代际或维护版本变化时停止本次新写入。

### 管理员 API

以下接口沿用 `worker_remote_v4_enabled`、管理员认证及现有维护表，无新增表、迁移或 Worker 协议。GET 使用独立只读会话的一条业务 SELECT，不取得业务行锁、不修改版本、不完成排空；有限语句/锁等待失败返回 503。响应为 `Cache-Control: no-store`。

| 接口 | 返回与边界 |
| --- | --- |
| `GET /api/workers/{id}/v4/status` | 保留原字段，增加 `observation_schema=1`、数据库 `observed_at`、protocol/enrolled、preflight/隔离布尔和能力、`active_attempts`、`has_unreconciled_attempts`、`current_maintenance_action`、`current_drain_operation`。Worker、计数和最近操作来自同一语句快照 |
| `current_drain_operation` | 当前版本匹配的 drain：`request_id/action/expected_version/result_version/same_actor`，否则 null；只允许原操作者显式继续原请求，不能跨操作者代领 |
| `GET /api/workers/{id}/v4/maintenance-operations/{request_id}` | `operation={request_id,worker_id,action,expected_version,result_version,created_at,same_actor}`，`current={worker_identity_id,maintenance_version,scheduling_state,observed_at}`，以及 `applies_to_current_version`；仅查询精确 drain/activate |
| `POST /api/workers/{id}/v4/drain`、`activate` | 保持 `{"request_id":"NONZERO_UUID","expected_version":N}`；服务器锁内重新裁决，不以浏览器观察授权 |

普通账号无管理员权限返回 403；Worker 凭据不能作为管理员凭据。错误 Worker、不存在或非 drain/activate 的精确回执统一 404。404 仅表示该快照没看到已提交行，不能证明仍在途的事务不会提交。只读响应不输出原维护行、恢复 binding、宿主断言、MAC、路径、正文或凭据。普通 `/api/workers` 列表合同保持。

### 显式排空、确认与激活

1. 新鲜观察为 active 时选择“申请排空”，核对节点、期望维护版本及请求 ID，再明确提交。服务端登记后拒绝新 Claim，原合法任务继续；页面只有独立 GET 看到同一操作和当前状态后才报告提交。
2. 仍为 draining 时，“已登记排空”不表示排空完成。原操作者等待未结归零后选择“确认本次排空”，明确重送同一 request ID 和原 expected_version；只有读到当前 disabled 才确认停用。刷新和轮询不会自动发送确认 POST。
3. B/C 恢复可能推进维护版本但保留 draining。旧 drain 必须拒绝。恢复完成且当前操作为 reconcile、容量 ready、零未结且无历史阻塞时，选择“确认恢复后排空”，用当前版本和新 request ID 登记本次确认，版本递增一次并转 disabled；未结或 reconciling 时不可确认。它不重做恢复、物理清理或结账。
4. 当前 disabled、在线、容量 ready、隔离检查通过且无未结责任时，选择“激活节点”并明确提交；服务器沿原全部资格核验。历史激活已提交但被后续维护替代时，页面会分开显示历史提交和当前状态，不再用旧 POST 的混合响应宣称现在 active。

disabled、零计数、在线及历史回执均不能证明物理停止、原目录/预算释放或当前 channel ACK；仍须按正常维护及 B/C 说明分别核对。

### 提交未知、刷新与权限变化

POST 前，本页只把 origin、当前认证范围、Worker/稳定身份、action、request ID 和 expected_version 暂存到该标签页的 sessionStorage 并回读核对；不保存 Token、证明或恢复材料。存储失败则不发送。一个 Worker 同时只保留一个未决意图。

W1-E 使用固定 `dlr-worker-maintenance-v2` 容器，每个认证范围最多两份、总计不超过 8192 字节，按 Worker ID 与稳定身份隔离。A 的未知请求不会被 B 覆盖，不驱逐未决记录给新动作腾位置。旧 v1 记录仅在严格校验、v2 持久化并回读完全相等后删除；迁入冲突或存储失败保留原记录并禁止新 POST。登出或 Principal 改变清除两版本；迟到回执只能清除它自己的精确 tuple。

超时、断线或 503 后先查询一次精确回执；未见行或读取失败继续显示“待确认”，不将其当作回滚。刷新页面仅恢复原 tuple 并 GET，不自动重发。用户可选择“重试同一请求”并再次确认，始终发送原 ID/版本/动作，不能自动换版本或新建意图。当前版本已被后续操作替代时，显示“原操作已提交，当前已被替代”。首次明确拒绝的 409 需重新核对当前状态；已有未知请求重试得到 409 再查到 404 时，仍保留原意图，因为这次拒绝不能阻止原在途请求迟到提交。权限变化和登出清除本地意图，后端鉴权仍是权威。其他管理员可观察原 drain，但不能冒用原 request ID 完成排空。

最近 C 处置始终显示固定未知原因和原 Execution/Attempt 终态，包括后续已激活时；“结清不重试也不回滚”，不代表业务成功。处置后的可信宿主与恢复操作仍按 [W1-B](remote-worker-wave1b.md) / [W1-C](remote-worker-wave1c.md) 执行，页面没有恢复或强制清账表单。

### 版本配对与回退

W1-D 使用现有 v4 开关与迁移链，不改变固定 React/Ant Design 基线。部署匹配 Web/Control；新 Web 遇到旧 Control 缺少完整观察 schema/回执接口时只读展示有限事实，提示管理版本需匹配并禁用新操作，不以旧 POST 200 替代独立读回。旧 Web 可继续原列表功能。停用新 Web 不回滚已提交维护意愿；若回退到没有旧 drain 版本围栏的 Control，应先停止管理写入并保留未决 tuple/审计，使用匹配版本向前修复，不能重置版本或覆盖旧数据库。

## 有界双固定 Worker（W1-E）

在私有 **Compose 环境文件**设置 `DLR_WORKER_V4_CONTROL_SLOTS=2`，并同步运行环境文件；Compose 的显式 environment 优先于 env_file。只接受 1 或 2，默认 1，不支持在线修改。每个 Worker 各用独立宿主、身份、HTTP/Broker 凭据、队列、runtime volume、journal 锚点及 Compose project；逐个 enrollment、真实 preflight、显式 activate。两个 Adapter 分别固定到 A/B。A 排空、离线或不兼容时，固定 A 的旧责任仍等待 A，不会转给 B。

Control 进程内仅有 K 个固定 Claim/Result 槽，每 Worker 最多一个。spawn 前登记，Claim 后原位保留同 child，stage/finalize 沿用同槽与预算；无保留句柄的合法结果重入也须先取 K 内名额，不重做 Claim。全局登记锁不跨网络、进程等待或业务事务，各槽独立 IO。proof、proposal、instance/generation、Attempt/fence、nonce 及维护请求不得跨节点使用。

有限 Control cgroup 须满足 `memory.max >= K × child_address_space_bytes + 128 MiB`、`pids.max >= K + 1`、swap 上限和当前用量为零，并保持现有 CPU/PID/连接总限制。实际内存和 PID 限额在首次使用后固定，变化即拒绝；默认 1.5 GiB Control、每 child 512 MiB 可覆盖两槽。Worker 宿主父级还须在全部容器硬限之和外给 keeper 留至少 0.05 CPU、16 MiB 及有限 PID 余量，仍须通过真实 preflight。

仅新 prepare 已核对当前 Control 权威与总资源、且确定在 spawn 和 Claim 事务前没有名额时，返回 HTTP 200 的既有六字段 envelope：

```json
{"decision":"DEFER","reason":"control_transport_busy","retry_after_seconds":1,"attempt_id":null,"cancel_requested":false,"payload":null}
```

该响应无 proposal/proof，不产生 Attempt、不消耗执行 Retry、不 ACK；沿原 consumer 有界退避。它只说明本次 prepare 未进入 Claim 事务，不证明旧未知请求回滚。已发送 finalize、结果提交未知、权威/资源不可确认或 IO/进程状态不明仍沿原 503/未知路径，不改写持久未知意图。A 可确认回收的断连/超时只关闭 A 槽；无法确认 child/creator 回收时保留计费并全局拒绝新 Claim/Result。共享 Control/DB/Broker 故障也不在节点独立维护承诺内。

升级先排空现有节点，核对业务、结果、本地记录和 Control child/creator 全部结束，再停 Control；保留数据库、身份和原卷，部署匹配 Control/Web 并显式启用 K=2。改变 K 或回退前同样须无在途/未知槽，不能用降 K、重启、换卷或删 pending 假装结清。回退旧 Web 前先确认 v2 无未决 tuple；回退为一节点时停止第二节点的新接收，保留其绑定、历史和排队责任，不静默转给 A。B/C 原恢复和阻断规则不变，无新迁移、协议升版、Pool、数据库资源预留或 HA。

上述 W1-E 步骤仍受原 Control 权威声明门禁约束：数据库精确绑定声明的 `image_digest`。W1-E 本身只提供专用新部署和匹配声明下的停启/K 配置变更。W1-F 的显式换镜像合同如下；新镜像不匹配时不能直接改声明、epoch、数据库或用旧 digest 冒充新镜像。

## 清洁排空后的停机换镜像（W1-F）

历史首个发布对为 W1-E main `a37c9d3e19985acddb1f3e33ff6d3c7337147270`、schema `0087_issue129_disposition` 到 W1-F 匹配制品、schema `0088_issue129_image_transition`。以下首跳须使用匹配的 **F 镜像**；原 G 二进制只允许新做下方 F→G；H 二进制只允许新做 G→H，不允许跳级。完整源/目标 SHA、各组件实际 digest 和制品来源在升级前冻结于私有记录及原 PR 交付证据，不把可变标签当 digest。除下方明确 G 发布对外，任意版本迁移和在线混版本仍不支持。

原数据库、Broker/vhost/队列、所有命名卷和 journal/runtime 根、cluster/epoch/proof key、Master Key/admin/legacy Token、四项指纹、K=1/2、Worker ID/凭据/固定绑定原样保留。新 Worker 仍使用原凭据和原卷，只产生新 instance/generation，保持 disabled 后分别显式 activate。转换不处理 queued、retry_wait 或未知责任，不改变业务结果，也不支持回退旧镜像。

### 停写与停止

1. 通过原 Adapter 设置停用所有 schedule，并在现有客户端/入口控制层阻断 manual、Replay/重试、配置和激活等新写入。保留源 Control 让已接受工作完成，然后逐个 drain 并用原请求确认 disabled。**drain 不阻止创建新 Execution**，不能替代入口停写。
2. 分别核对无 queued/running/retry_wait、未结 Attempt/cleanup、占用 Slot、活动输入 Lease、未发布/leased Outbox、未处置 Infrastructure DLQ；Broker ready/unacked/确认未知、Worker journal/runtime、Control retained/closing/creator/IO 及浏览器 pending tuple 也须清洁。已结失败/取消、B/C 历史和独立 Hold 保留。不能 purge、取消、删行或换空目录凑空。
3. 用原 Compose 停止全部 Control（包含 scheduler/Relay/reconciler）与全部 Worker，禁用自动重启源；可信操作者从原生 Docker/Linux 核对整个进程树、cgroup 和 IO 结束、宿主/原卷独占。保存原容器/boot/挂载和实际镜像证据。CLI 不远程证明这些事实，人工摘要也不构成证明。
4. 数据库管理员将 runtime role 设为 `NOLOGIN`，排除残留连接、其他写入口和 prepared transaction；`NOLOGIN` 本身不终止既有会话，CLI 不任意 kill 会话。保持单操作者、唯一 owner 维护连接，并禁止其他管理员同时修改角色/授权/复制或写库。按 DB 时间等待所有旧 nominal lease、guard、已签证明最晚期限过去，保留租约行，不提前改时间。

### 私有输入与命令

保留原 `source.json`，准备只改变 `image_digest` 的 `target.json`；沿用原 proof-key 和 owner 环境的三个凭据。三个 JSON 文件均为本机实际运行 UID 所有、0600、单硬链接、非符号链接且不超过 16 KiB；未知/重复字段拒绝。私有 `intent.json` 采用以下封闭字段：

```json
{
  "operation_id": "REPLACE_WITH_NONZERO_UUID",
  "previous_id": null,
  "source_schema": "0087_issue129_disposition",
  "target_schema": "0088_issue129_image_transition",
  "source_sha": "a37c9d3e19985acddb1f3e33ff6d3c7337147270",
  "target_sha": "REPLACE_WITH_VERIFIED_40_LOWERCASE_HEX_SHA",
  "control_slots": 2,
  "evidence_sha256": "REPLACE_WITH_64_LOWERCASE_HEX"
}
```

第一跳只接受空前序；回执表以唯一根和唯一后继保存链，旧请求不能靠 digest 往返再次匹配。`control_slots` 填源部署冻结的原 K（1 或 2），check/apply 同时核对目标 owner 配置，不在升级中改变 K。`evidence_sha256` 仅为操作者已核对的原生记录摘要，不是远程硬件、镜像供应链或物理停止证明。intent 摘要另外绑定源/目标声明的非秘密字段；明文凭据不进入回执或输出。

下面每次运行均使用**目标镜像**和原隔离网络，owner 环境的 `DLR_WORKER_REMOTE_V4_ENABLED=true`、runtime role/admin role 配置沿用原值；维护进程使用短连接，不启动产品后台。密钥和密码只从私有文件/环境读取：

```sh
docker run --rm --network DEDICATED_NETWORK --env-file /ABSOLUTE/PRIVATE/owner.env \
  --mount type=bind,src=/ABSOLUTE/PRIVATE/control,dst=/run/dlr-private,readonly \
  TARGET_CONTROL_IMAGE_DIGEST python -m dlr.control.control_identity_init_v4 check \
  --source /run/dlr-private/source.json --target /run/dlr-private/target.json \
  --intent /run/dlr-private/intent.json
```

`source_checked` 后，沿原 Alembic 入口在同一停机围栏下运行 `alembic upgrade 0088_issue129_image_transition`。此迁移只新增回执元数据，不改 authority；数据库 schema 已新而 authority 仍旧时，两个版本都没有正常启动资格。迁移提交不明先独立读回 revision/表结构，精确源态可重跑同一迁移，精确目标态向前进入 apply，混合/未知保持停机。

再次运行同一个 check 应返回 `ready_to_apply`。将上述命令的 `check` 分别替换为 `apply`、`observe`，保持原两个声明和原 operation ID。apply 使用 READ COMMITTED 顶层事务，沿 exclusive schema gate → authority → receipt/旧实例固定锁序，复核角色、会话、清洁责任、所有旧期限和源凭据。CAS digest/schema、唯一回执及永久 revoke 旧 Control 实例同事务提交；不改 Worker 维护版本或业务账本。连接/锁等待有界至 5 秒，语句不超过 10 秒；失败整笔回滚，不能用 CLI exit 0、自读或 `RETURNING` 证明提交。

| 输出状态 | 允许动作 |
| --- | --- |
| `not_installed` | 源 revision 下尚无回执表；只说明元数据未安装，可核对后执行指定迁移 |
| `not_found` | 本次 ID 未在独立快照读到；不等于旧未知请求已经回滚。重新检查原前序、清洁与停写前提后才可显式再次 apply |
| `committed_current` | 完整 intent、回执和当前 authority/链末端一致，可以继续安装该目标；已提交重复 apply 不再 revoke 新实例 |
| `committed_superseded` | 仅证明历史提交，禁止按旧回执安装旧声明；停机核对当前转换，不自动追随其他目标 |
| `refused` / `unknown` / 读取失败 | 维持停机和角色围栏，保留原 ID 与输入；不猜测回滚，不重新 init、不反向 CAS |

apply 会关闭写连接后另开只读事务读回，COMMIT 回复丢失也按同一规则观察。只有独立观察 `committed_current` 才可继续。`committed_at` 为回执在事务内记录的 DB 时间，提交可见性以独立事务读回为准。

### 原子安装与恢复服务

由可信操作者在**原私有目录**写入目标声明临时文件，设置原 UID/0600，fsync 文件后 rename 到原 `control.json`，再 fsync 目录并读回核对。DB 事务不做文件 IO。安装及启动前再次 observe；并行部署操作者不在支持范围。提交后安装前中断，可复用原 ID 只读确认并继续同一安装。schema 前进或转换提交后只支持向前完成，不能降 schema、覆盖旧备份或换回旧镜像。

核对目标 Control/Web/全部 Worker 为匹配发布，原配置和 K 未改；owner 连接退出、旧启动源仍被阻断后恢复 runtime 登录。沿原 Compose 启动单 Control 和原 Worker，核对信任、稳定 ID、原卷和新 instance/generation，仍 disabled；逐个显式 activate 后恢复获授权的 schedule/manual 入口。用新业务键分别核对目标效果、唯一 Attempt/Slot、cleanup 和 ACK，不能以 online 或版本页代替。

普通 runtime/Worker/Web admin Token 无转换权限。回执保留 operation/前序、cluster/epoch、源目标 digest/schema、非敏感 intent SHA-256、事务 ID 和 DB 时间；无自动 TTL。有回执或目标 authority 引用时 Alembic 拒绝降级删除，原 cluster/epoch/proof key 的 ALWAYS 不可变触发器保持。默认 v3 不进入此路径，B/C 的异常恢复资格不扩展。

## 受限 JavaScript 与 F→G（W1-G）

### 显式语言模式

Worker 私有 Compose 环境中的 `DLR_WORKER_LANGUAGE_MODE` 默认是 `python`，不要求安装 Node。需要执行 JavaScript 的节点显式设为 `python_javascript`；未知值拒绝启动。原 Python 任务、K=1/2、固定节点和 Slot 0 保持。

双语言节点先完成原隔离预检，再在同一实际 Sandbox 和原标准任务硬限制内运行固定 `/usr/local/bin/node` 的完整 ESM harness 小样本，核对 Node 22、隔离身份/namespace、JSON 结果与物理清理。Node 缺失、来源路径可写、版本不符、资源或清理失败都不发送新 ready，不自动降级成 Python-ready。可信部署须保证镜像和运行时根不可被 Adapter 或其他低权进程替换。

ready 的可选 `capabilities` 字段只接受 `['python']` 或 `['python','javascript']`，缺字段只表示 Python；重复、乱序或其他值拒绝。新 instance 清空旧能力；心跳不授予新能力、不解除 disabled。报告是可信 Worker 的本机观察，不是远程硬件证明。系统状态显示已报告语言；过期/未知观察不开放激活，普通用户仍只读。

JavaScript 沿用 ESM `handle(context, input)`：

```javascript
export async function handle(context, input) {
  context.logger.info("normalizing input");
  return { value: input?.value ?? null, mode: context.config.mode ?? "default" };
}
```

只承诺 Node 内置模块和镜像内运行时，不安装 npm 包，不创建 nodeenv、依赖 cache 或 node_modules。`requirements` 必须为空；dependency check、文件、builtin snapshot、credential bindings、secrets 及其他触发方式仍拒绝。JS 按 npm 包源核验 builtin 限制，Python 按 PyPI；已接受 Execution 使用原冻结材料/凭据快照，不受后来的默认包源覆盖。

新接收和 schedule 启用/触发都要求固定目标具备对应封闭语言能力；容量忙、维护或暂时离线仍按原排队合同处理，不 fallback。派发和最终 Claim 再核对冻结责任及当前能力，不兼容时有界等待且不创建 Attempt、不增加执行重试。Worker 对消息、payload、任务指纹、目录句柄和预算再核对，在原有界 Claim child 内写 `adapter.mjs` / `.dlr-harness.mjs`；封存 argv 使用固定 Node/harness/workspace/adapter 路径，不经 shell，也不继承 `NODE_OPTIONS`、`NODE_PATH` 或 npm 环境。

JS 使用原监管器、CPU/memory/pids/tmp/nofile/no-swap、结果证明、物理清理、结账和 ACK 顺序。B/C 仅增加 JS 单语言允许文件集合：实际 inventory 可为 Python、JS 或共同文件子集，旧 Python 缺部分模板文件仍可读；混合语言、未知文件、链接和不匹配 binding 拒绝。没有完整可信 stopped 的 partial staging 仍为 unknown，不因语言扩展而获得新恢复资格。

原执行截止也可能在 Sandbox 准备或放行前到达。只有监管器独立观察到原执行定时器到期、没有同时出现父进程/控制通道/辅助进程异常，且原监管器回收、cgroup 归空和输出排空均已确认，才沿原超时结果、清理和 ACK 链完成；不延长执行预算。仅看到准备失败、进程退出或本地时间超过截止都不够，仍保留 unknown。修复不回填历史 stopped，也不改变旧未结记录的恢复资格。

### 精确 F→G 后继

G 的新 apply 只接受 F main `0417b82c4ab6f76aefdf3eafe38327f46cb18358` 或原交付中已核实同产品输入的 F 制品；各组件真实 digest 与 G 目标 SHA/digest 映射须在操作前冻结。源、目标 schema 都是 `0088_issue129_image_transition`，**不运行新迁移**。相同 schema 或 Node 版本不表示其他发布对兼容。

沿上方全部停写、清洁责任、真实进程/IO 停止、租约上界、唯一 owner 和 runtime NOLOGIN 前提操作。保持原库、Broker、身份、凭据、K、卷、四指纹、epoch 和 key：

| F 来源 | 新 G 意图 | 允许回执 |
| --- | --- | --- |
| 合法 fresh-F，当前 F authority，回执为空 | `previous_id=null` | 唯一 G root |
| 恰有一个合法 E→F root，当前 authority 等于其 F 目标 | `previous_id=原 root ID`，同时提供原 E 声明与原 E→F 意图 | 唯一 G child，原 root 保持 |
| 任意更多链、未知/陈旧前序、摘要或当前 authority 不匹配 | 拒绝 | 不新增、不回退 |

原表没有 source/target SHA 原文字段。后继通过有界、owner-only 私有文件重建原 E→F 意图摘要，并与回执精确比对；原意图的 F 目标就是本次 F 源声明。缺少原件或无法核实制品来源时保持不支持，不能改摘要、SQL 补回执或换空卷凑前序。

G `intent.json` 仍使用上述八字段：`source_schema` 与 `target_schema` 均填 `0088_issue129_image_transition`，`source_sha` 填已核实 F SHA，`target_sha` 填本批 G 的完整 SHA，`operation_id` 为新 UUID，K 不变，前序依来源填写。fresh-F 使用原三个文件参数；有前序时每次 check/apply/observe 都带以下两个附加文件参数：

```sh
docker run --rm --network DEDICATED_NETWORK --env-file /ABSOLUTE/PRIVATE/owner.env \
  --mount type=bind,src=/ABSOLUTE/PRIVATE/control,dst=/run/dlr-private,readonly \
  G_CONTROL_IMAGE_DIGEST python -m dlr.control.control_identity_init_v4 check \
  --source /run/dlr-private/f-source.json --target /run/dlr-private/g-target.json \
  --intent /run/dlr-private/g-intent.json \
  --previous-source /run/dlr-private/e-source.json \
  --previous-intent /run/dlr-private/e-to-f-intent.json
```

check 应返回 `ready_to_apply`。保持全部参数和原 ID，将 `check` 换成 `apply`，关闭写连接后的独立 observe 必须为 `committed_current`，才可按上方 fsync/rename 流程安装 G 声明及匹配 Control/Web/全部 Worker。apply 按原锁序核对完整允许链及当前 latest，原子提交源 CAS、旧 Control 实例永久 revoke 和 append；同 ID 已提交重入不再 fencing 新实例。新请求不能用摘要/目标 digest 相等绕过陈旧前序或 ABA。

| 同 schema 时点 | authority / 回执 | 启动与操作 |
| --- | --- | --- |
| 转换前 | F / 空表或原 F root | 保持停机，G 声明拒绝；不伪造迁移中间态 |
| G 提交后、安装前 | G / G root 或 F root→G child | 旧 F 声明拒绝；只按原 G ID 向前安装 |
| 安装并确认 current 后 | G / 同上 | owner 退出后恢复 runtime，启动原身份/原卷；各 Worker 仍分别显式激活 |
| 只读原 E→F 历史 | 保留原 root；G 已为 latest | `committed_superseded` 只证明历史，不能回装 F；旧 F CLI 不用于处理 G 后继 |

提交未知、安装中断和旧回执仍沿原只读状态表处理，不支持 G→F 反向 CAS、数据库回档覆盖或混版本滚动。真实发布对和升级后 Python/JS 混合执行、JS schedule、目标效果、Attempt/Slot/cleanup/ACK 的验收回执补原 PR 与私有记录；文档命令本身不代表验收通过。

## 绑定业务凭据与 G→H（W1-H）

### 配置与许可

沿原 Credential CRUD、Adapter owner/admin 绑定权限、Fernet/HKDF 和 Master Key 使用四类字段：password 的 username/password、token 的 token、access_key 的 access_key_id/access_key_secret、secret 的 value；旧 access_key/secret_key 字段别名仍可读。不增加秘密服务、数据库迁移、策略页面或通用环境变量入口。

Worker 私有 Compose 环境默认 `DLR_WORKER_BUSINESS_CREDENTIALS_MODE=disabled`；仅显式 `bindings` 启用。新 ready 可缺省严格 bool `business_credentials_v1`，缺失为 false；Control 保存在独立能力键，完整提交观察失败不授予 ready，新 generation 清除旧观察。此能力不授予 Credential 权限，语言、名称、标签和 Worker HTTP/AMQP 身份也不授予权限。

Control 的私有运行环境可配置 `DLR_WORKER_BUSINESS_CREDENTIAL_POLICY_FILE=/run/dlr-private/business-credentials.json`，复用现有私有只读挂载。文件必须为实际 UID 所有的 0600、单硬链接普通文件，绝对规范路径且无符号链接，最多 16 KiB，采用以下封闭 JSON（ID/UUID 均为示例）：

```json
{"version":1,"workers":[{"worker_id":1,"worker_identity_id":"11111111-1111-4111-8111-111111111111","credential_ids":[10,11]}]}
```

最多两个不同 Worker，每项同时匹配稳定数字 ID 与 identity UUID，最多 16 个不同正整数 Credential ID；重复键/身份/ID、未知字段、bool/字符串/float ID 拒绝。缺省策略只拒绝携凭据任务；已配置而非法/缺失时拒绝启动。单 Control 启动冻结策略及 digest，只经私有 IPC 传递同一非秘密快照，运行中不热重读文件。许可变更应停止新责任、排空、停机并受控重启；不改四项 namespace/capacity 指纹。

每 Execution 最多 16 个绑定；env_key 为 `[A-Za-z_][A-Za-z0-9_]{0,63}` 且唯一。单值非空、无 NUL、合法 UTF-8，最多 4096 bytes，全部绑定值合计 16384 bytes；引用的完整 ciphertext 单项最多 32768 bytes、不同 Credential 合计 65536 bytes。数据库先查引用计数/长度，再在有界 Claim child 解密。接收、schedule 启用/触发、派发与最终 Claim 都重检原引用、目标能力和部署许可；新任务不兼容时不承担 Execution/Admission/Outbox 责任，旧任务保留原路由有界等待。

### 值时点、结果与本地责任

Execution 冻结 Credential ID/field/env_key 及原不可删除引用，不冻结接受时真值。Claim 按原锁前缀后追加 Credential ID 升序行锁；prepare 仅向获准 Worker 的有界 child 提供 tentative 材料，不能运行；finalize 重建精确 wire，有效真值改变须回滚、零有效 Attempt。提交后的 Attempt 保持该次值；后续合法重试可读新值。删除/更新平台凭据不会即时收回已发值，上游撤销与原取消是独立操作。

Worker 从复制实例证明到原 Claim child 完整接收 tentative 回复，使用同一原续租锁防止旧证明与新 candidate 竞争；仍受原本机期限、最多 5 秒传输预算和 stop/失权信号约束。交换结束或异常即释放锁，再重检资源、持久意图和最终 Claim；不放宽服务端证明验证、不延长租约或改动未知责任/ACK 条件。

原固定 argv/harness 的 sealed launch 仅加入本次绑定 `DLR_SECRET_<env_key>`，不继承宿主同名值、额外秘密或 NODE_OPTIONS 等环境。父进程只持同一 launch 内核对象的有限 opaque dup fd 与身份/尺寸；原结果 child 在清理前读取它，所有 reader/IO 确认结束后关闭最后引用再结账。无需普通秘密文件、新秘密进程、重启后重新解密或扩大原资源档。

原 Agent 有界 stdout ring 仍可能暂存业务回显的原值，不将其写入普通日志/进度/诊断。结果 child 先对完整非截断流、JSON 字符串键和值按长值优先做字面替换，再计算 size/preview/MAC；原始截断 stdout 仅给 `[REDACTED]`，截断/非法 JSON 不给原文 preview。净化后键碰撞整份 output 省略、preview 为空、`output_redaction_conflict`，保留成功且不因此重跑。fd/净化/物理停止未知时不外发原文、不伪造清理/ACK，B/C 格式与恢复资格不变。这不是任意编码/拆分变换的 DLP，也不隔离恶意授权代码、root 或宿主管理员。

### 精确 G→H 停机后继

H 新 apply 源仅为 G main `a4a9da204659c36ec6b843bb28c8f13e5f355582`，或原交付核实的 `530ab2e8e5e7efaa7a1492a2c754a461cb6d4ba7` / `8d216d3a052a6643fdf1bee74b1538e1ddd8a891` 同产品输入。原早期未修复 G 不在来源内。目标 H 的完整源码 SHA、构建输入与各组件真实 digest 在私有升级记录冻结；不复用旧 digest 冒称新构建。schema 始终为 `0088_issue129_image_transition`，**不运行新迁移**，K 保持原 1 或 2。

| 原 G 历史 | H 后唯一链 | 本次额外私有文件 |
| --- | --- | --- |
| 经核实 fresh-G、空转换表 | G→H，一笔 | 无 |
| fresh-F 经原 G 原生转换 | F→G→H，两笔 | previous-source / previous-intent |
| 原 E→F、F→G 连续回执 | E→F→G→H，三笔 | previous-source / previous-intent，加 root-source / root-intent |

保留原三文件 `--source G.json --target H.json --intent G-H.json`；按上表有前序时，每次 check/apply/observe 使用完整原文件：

```sh
python -m dlr.control.control_identity_init_v4 check \
  --source /run/dlr-private/G.json --target /run/dlr-private/H.json \
  --intent /run/dlr-private/G-H.json \
  --previous-source /run/dlr-private/F.json --previous-intent /run/dlr-private/F-G.json \
  --root-source /run/dlr-private/E.json --root-intent /run/dlr-private/E-F.json
```

fresh-G 省略后两行；只有 F→G root 省略最后一行。不接受半套/重复标志或任意递归文件列表。八个 intent 字段和原摘要算法不变；G-H 的 source/target schema 均填 0088，previous_id 绑定当前 F-G operation 或 null；原回执不存 SHA，须用原始意图与声明重建核对，不能从表猜版本。按 previous_id 重建因果链、固定 UUID 锁序最多取四行拒绝更长历史；H 目标 digest 不得等于任何已提供祖先 source/target，target SHA 不得退回 E/F/G，检查/写事务/读回均验证。

沿前述所有停写、物理进程/IO 停止、旧租约上界、原卷独占、唯一 owner/runtime NOLOGIN、无 prepared xact、Broker/journal/Admission/cleanup 清洁前提。exclusive gate→authority→完整 chain→旧实例下 CAS/revoke/append 同一事务，关闭 writer 后原 operation 独立只读观察；只有 `committed_current` 才前向安装。原 E/F/G 回执可只读区分 current/superseded，H 拒绝旧 E/F 的新 apply，也拒绝重新 apply 历史 E/F intent；superseded 不授权安装。

声明和策略分别沿原目录 fsync/rename 安装，不是跨文件原子事务；保持入口关闭，全部文件及预冻 policy digest 读回一致后才启动匹配 Control/Web/原身份原卷 Worker，新 generation 仍 disabled 并显式 activate。预定非空策略缺失不能用默认空策略冒充安装完成。原 DB、Broker/vhost/队列、cluster/epoch/密钥/四指纹、Worker ID、Credential ciphertext/引用/历史和卷保持。提交前且源权威未改可中止并按 G 恢复；提交后不回退 G。

本批真实验收只重建一套合法新 F→G 来源，保留两笔 G 无凭据已结历史后同库追加 H；旧含 unknown 归档不能清账作来源。两个不同 Linux boot 的 Worker VM 最多各 2GiB，源目标复用、结束回收；不常驻 dlr-preview。验收使用非生产 canary，oracle 只记业务键/鉴权布尔/效果数，泄漏扫描只输出布尔/计数。真实结果与精确制品映射由原 PR/私有证据记录，不能以 HTTP 200、online、文档或 CI 冒充完整结算。

## 受限文件输入与 H→I（W1-I）

### 显式输入模式与失败责任

Worker 私有 Compose 配置默认 `DLR_WORKER_INPUT_MODE=disabled`；只有显式改为
`managed_files` 并重新启动、通过本次受限文件写入/读回/清理探针，才报告严格 bool
`managed_files_v1=true`。新实例清除旧报告。该能力不是 Artifact 权限，也不授予 H 的
业务 Credential 许可。I 的完整真实升级和业务验收仍须绑定实际 PR/制品，本文不代替验收。

| 项目 | I 的范围 |
| --- | --- |
| 来源与语言 | 单 Control、K=1/2、strict fixed/Slot 0，无依赖 Python/JavaScript，manual/schedule |
| 文件 | 每 Execution 冻结 1–4 个；允许空文件；单项 ≤1MiB、合计 ≤4MiB；同时满足原配额、Claim wire 和全部 staging/tmp 预算 |
| 内容与名称 | 复用原后缀白名单、受控 mount 名和 manifest；原字节交付，不安装解析器、不解压、不接受 URL/宿主路径/共享卷 |
| 下载 | 独立 v4 GET；TLS、当前运行凭据、实例和 Attempt 双证明、fence、Claim Token、冻结 Artifact/Lease 同时核验；拒绝 v3 旁路、query Token、Range、重定向、压缩 |
| 期限与资源 | 复用同一已持有的 Control Claim/Result child 和 Worker staging child；顺序最多 4 次完整 GET，无文件级重试/续传；块 ≤64KiB、单侧最多两块在途；普通续租不延长已准入流 |

Control在其finalize入场前冻结同一slot/child的单调时钟准备截止，Worker也在发起finalize前
独立冻结5秒上限；同时取不可续期的claimed_at+5秒及原执行/证明期限，不能从首次GET或staging
重算 5 秒或按四个文件累加 20 秒。Agent 不读取或哈希业务文件正文。Control child
先验证已打开对象，Worker child 独立核对 size/hash；完整集合及 manifest 校验并同步后
发布 input（文件 0444、目录 0555），manifest 最后发布，业务启动门保持关闭直至成功。
原 staging 与 Sandbox 副本并存计入预算。权限只是防误写，同 UID 并非强只读隔离；
业务仅得到独立副本，不能改变 Control 原件或冻结快照。

文件准备失败保留真实 Attempt、原 Retry Policy/截止和退避。只有同一 staging child
及 creator 已回收、IO/fd 已关闭、原 guardian 确认 cgroup 为空，才以明确的“业务未启动”
回执走原 v4 结果提交/独立观察、H 净化、cleanup、本地结账和自身 ACK；不伪造业务 exit。
任何物理关闭、归属、授权或提交未知仍保留 Slot/Lease/journal/K/charge，停止新接收。
HTTP 断开、超时或租约到期本身不允许清账，也不追溯释放原 unknown。

文件的 prepared/stopped/terminal 使用私有 version 3，只供同一次活动责任监督与清理，
最多记录 4 项 mount 名、size/hash 和对象绑定，不存原文件名、正文或凭据。B/C 的跨进程
恢复明确拒绝文件 version 3；崩溃残留保持 unknown，不重取文件、不重跑业务。
正常活动 owner 在物理停止、持久清理意图及 cgroup 移除后，按冻结绑定删除最多四份原输入
副本，再复用原空目录清理；文件绑定、unlink、fsync 或 fd 关闭不明仍保留责任，不发送清理成功或 ACK。
none/JSON 仍使用原 prepared/stopped v1、正常 terminal v1、C 处置 terminal v2。
启用前应接受这一恢复边界，不能靠删除卷、journal 或换身份继续验收。

### 唯一 H→I 停机后继

schema 保持 `0088_issue129_image_transition`。只认 H main
`9d7cd67f483950d4a6f96ebc9342005636f61348`、最终 PR HEAD
`ed925887f28704001117825d0d46488dacbfe595`、原真实 H 验收制品
`72f2aa23bd9b680f820d16b82283b950533c6775`。对实际采用的来源逐个核对原声明、构建输入和
镜像 digest，私有冻结目标 I 的完整 SHA/digest；不能按任意同 tree 或空表推定资格。

| 原 H 历史 | I 后唯一链 | 原私有前序文件 |
| --- | --- | --- |
| 合法 fresh-H | H→I | 无 |
| G→H | G→H→I | previous-source / previous-intent |
| F→G→H | F→G→H→I | previous，加 root-source / root-intent |
| E→F→G→H | E→F→G→H→I | previous、root，加 origin-source / origin-intent |

完整最长来源的同一套原文件分别用于 check/apply/observe：

```sh
python -m dlr.control.control_identity_init_v4 check \
  --source /run/dlr-private/H.json --target /run/dlr-private/I.json \
  --intent /run/dlr-private/H-I.json \
  --previous-source /run/dlr-private/G.json --previous-intent /run/dlr-private/G-H.json \
  --root-source /run/dlr-private/F.json --root-intent /run/dlr-private/F-G.json \
  --origin-source /run/dlr-private/E.json --origin-intent /run/dlr-private/E-F.json
```

短来源按表省略最早的整套标志。八个原 intent 字段和历史摘要算法不变；重建完整因果链，
最多三套前序/四笔转换，查询第五行即拒绝；检查全部祖先 ABA、latest、authority 和原相邻
声明，不能只放宽 LIMIT。I 二进制拒绝 E/F/G 的新 apply；原合法历史仅只读观察。

先停止所有触发/接收，清洁排空 queued/Outbox/Attempt/cleanup 及本地责任，显式 disabled，
真实停旧进程/IO 并等待原授权上界；原 unknown 阻断升级。保持原 DB/卷、身份、密钥、
四指纹、K、HTTP/AMQP 凭据、H 策略/ciphertext/引用及历史。
原 gate→authority→chain→旧实例锁序下 CAS/revoke/append；提交未知只读 observe，只有
`committed_current` 才前向安装原文件。启动匹配 I 制品后获取新 generation，重新探针、
报告能力，并另行显式 activate；提交后不回退旧二进制/DB。I4 仍需两个不同 Linux boot
的临时 Worker，最多各 2GiB，源目标复用且结束回收；dlr-preview 不常驻。

## 诊断与停止边界

| 现象/代码 | 核对与处理 |
| --- | --- |
| `worker_remote_deployment_incompatible` | 存在旧身份或非阶段执行责任。保留原模式和数据，拒绝原地切换 |
| `worker_remote_control_unavailable` | 检查私有文件 UID/0600、声明/版本、数据库角色和其他 Control；查看 Control 平台日志，不输出完整环境或密钥 |
| 激活 409 | 重新读取管理员版本、disabled/ready/online 状态及未结责任；保留原操作记录 |
| 激活/维护 503 | 提交或确认可能未知，保持请求 ID，先读回状态和审计；不将未知响应解释为已完成 |
| `worker_task_capability_unavailable` | 按响应 `params.reason` 修正新任务；已有快照保留责任，不改写冻结内容 |
| `worker_cleanup_unsettled` | 状态 API 仍有未结 Attempt；保持 draining，核对实际进程、结果和清理 |
| Agent `recovery_required` | 保留原 journal/runtime 和服务端责任，停止自动启动；本阶段没有历史自动恢复或强制清账 |
| Agent `worker_unavailable` / `execution_unconfirmed` | 查看已有 phase/stage 和异常类型诊断，对照身份、TLS、cgroup、目录权限及本地残留；输出、预算、清理或 ACK 未确认时保持未知 |

普通运行操作共享 Token 命名空间锁，凭据签发/撤销仍独占，Worker/Attempt 行锁和证明期限仍逐项验证。Control 续租进行中只保留上次确认的期限；提交未知、旧期限已过或配置变化后拒绝授权。上述机制不提供多 Control、接管或异常后的自动恢复。

凭据签发/撤销碰到正在进行的普通操作时可能保守返回 503。先按原身份读回状态和审计，确认没有签发/撤销提交后再退避重试；不能直接把 503 当作事务回滚。激活本身属于共享操作，不再因另一个合法普通操作持有同一命名空间围栏而必然失败。

## 实际资源归属

Control 为一次 Attempt 保留一个有限资源的 Claim 子进程，在 Claim 结束后销毁旧业务帧，只留下小型归属绑定。同一子进程依次处理该 Attempt 的结果 stage 和 finalize，避免重复启动解释器占用两秒保守时钟区间。它不是跨任务进程池，也不赋予跨 Control 的结果亲和/接管能力。

保留子进程只保留资源，不保留授权：stage/finalize 必须重新核对当前 Control 权威、身份、证明和原 intent 期限。父进程从 Claim 句柄向结果句柄移交原 pidfd、socket 和创建者；在确认原子进程回收前不结束其 IO/资源名额。超期、失权或回收不明时停止或保持未知，不能重新创建一个句柄冒充原进程。

Worker 的任务解析/结果构造进程在接收业务材料前施加地址空间限制；参考配置实测为 256 MiB，结果 HTTP 传输子进程为 128 MiB。Control 对应子进程为 512 MiB。子进程启动到设置限额之间、Agent、监管器及实际业务进程同时受外层 cgroup 的有限内存/CPU/PID 和零 swap 限制。业务另有独立的 512 MiB 任务 cgroup。父进程与解析/传输副本不能只靠 wire 字节数计账。

## 阶段验证及限制

| 覆盖 | 已核对事实 | 边界 |
| --- | --- | --- |
| 真实运行 | 两个独立内核的 Linux 宿主；真实 TLS、独立 Worker AMQP 账户、固定 Python 任务及受控 HTTP 目标；数据库结果、终态日志、Slot、目录/cgroup、清理结算与 Broker 累计 ACK 对账 | 一个 Control、一个活动 Worker；不是多节点 HA |
| 正常维护 | 工作中 drain 不停止合法旧任务；归零 disabled；同一容器/持久目录正常停启后 generation 更新且仍 disabled；显式激活后再次执行 | 不支持带未知残留的自动恢复 |
| 正常终态 | 成功、明确业务失败、执行超时均完成结果、清理和 ACK；同一 Worker 进程预算可再次使用 | 信号退出或原因不明的失败不会被伪造为普通业务失败 |
| 两类真实故障 | 运行中撤销使旧心跳被拒、原进程树停止、完成目标未发生；另通过有限 TLS 代理延迟结果回执，独立确认结果已提交但无清理/ACK；原目录带残留重启返回 `recovery_required` 且记录未变 | 仅覆盖这些受控故障，不声称全部网络、时钟或恢复场景 |
| 资源 | 复杂嵌套 JSON 输出逐项一致；实际父子进程归属、地址空间限额、有限 cgroup 和零 swap；内存压力/异常退出后保留未结责任，不提前 ACK | 样本峰值不是完整上界；不把未捕获的 OOM 事件宣称为已确认 OOM |
| 迁移和兼容 | 全部候选迁移从基线升级及新建；旧身份、绑定、版本和执行历史保持；有责任的降级拒绝；默认 v3 的直接 Claim、结果/取消及文件引用回归 | 不提供旧部署直接切换到本阶段远程模式 |

实际 SHA、镜像、拓扑、首败、重试和原始验收记录由阶段 PR 与私有验收记录绑定。公共文档不包含私有地址、账号、凭据、宿主路径或日志原件。未实现的完整 Wave 1、Pool、共享存储和 HA 仍由 #129 跟踪。
