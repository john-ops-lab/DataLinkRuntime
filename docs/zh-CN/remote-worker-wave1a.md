# Wave 1-A 远程固定 Python Worker

本阶段是专用新部署的受限功能：一个 Control 应用进程、一个固定 Worker、单并发执行；支持标准库 Python、none/JSON 输入及普通手动/定时任务。候选已完成两个独立 Linux 宿主上的真实运行、正常维护/停启和直接故障验证；最终代码审查及 CI 门禁以阶段 PR 为准。正式合同见 [remote-fixed-python-worker](../../openspec/specs/remote-fixed-python-worker/spec.md)。

不支持第三方依赖、托管文件、内置包、业务凭据注入、其他语言、Webhook/事件/批次、旧身份迁移、异常后的自动恢复、Pool 或 HA。不能把业务凭据写入代码来绕过限制。#129 在阶段 PR 后保持开放。

## 候选代码与依赖

| 入口/职责 | 保留的实现 | 理由 |
| --- | --- | --- |
| Control 应用 | `control/app.py` 中显式开启的 identity/enrollment/instance/activation/attempt/claim/result/cleanup v4 API | 只有此处实际挂载的业务入口属于阶段运行路径 |
| 独立 Worker | `worker/agent_v4.py` → `consumer_v4.py` | 完成可信启动、真实 preflight、实例心跳和逐条 Broker 投递；配置无 Broker 时仅对账，不代表可执行 |
| 有预算领取 | `claim_preparation_v4.py`、`claim_http*_v4.py`、`claim_task_child_v4.py`、Control `claim_{coordinator,child,envelope}_v4.py` | 解析准备与最终 Claim 分开；未知提交与明确容量等待不得混淆 |
| 执行 | `execution_supervision_v4.py`、`attempt_session_v4.py`、`attempt_supervisor_v4.py`及`attempt_supervisor_child_v4.py` | 监管子进程由父进程按文件路径启动，不是未使用文件；业务使用既有 Python harness/Sandbox |
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
  attempt_result_intents_v4, worker_cleanup_intents_v4 FROM dlr_wave1a_runtime;
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

四个指纹分别对应已冻结的存储命名空间和容量配置，使用其规范化声明的 SHA-256；它们只作声明一致性比较，不能证明共享存储或物理容量。集群、epoch、公钥、密钥和声明需持久保留，不能在每次启动时重新生成。初始化只创建首次权威或核对完全一致的已有权威，不提供升级、替换和旧部署迁移。

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

任务保存标准库 Python 版本并绑定该 Worker，使用 none/JSON 输入。手动 `POST /api/adapters/{id}/executions` 和已启用定时任务共用接收门禁；语言、依赖、文件、内置包、凭据或非阶段触发方式不支持时，在接受责任前返回 `worker_task_capability_unavailable`。旧快照仍在最终 Claim 复核，保留原责任并给出等待原因。

新部署可能为新建 Adapter 预填演示凭据绑定。阶段任务应在接受执行前将其绑定集设为空；`PUT /api/adapters/{id}/credential-bindings` 的请求体为 `{"bindings":[]}`，不会删除平台凭据。门禁不会静默忽略已配置的绑定。

正常排空按以下顺序进行：

1. 保存状态 API 返回的 `maintenance_version`，向 `POST /api/workers/{id}/v4/drain` 发送新 UUID `request_id` 和该 `expected_version`。新 Claim 被拒绝，已有执行仍可完成结果和清理。
2. 保留原请求体；重复同一排空请求核对完成情况。存在未结 Attempt 时保持 draining，责任归零后返回 disabled，不重复递增维护版本。
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
