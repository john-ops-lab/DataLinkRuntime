# B4 隔离进程故障探针

仅用于 #222 的独立故障轮次。默认不启用，不修改后端源码，不注册产品入口，不参与正常容量轮次；使用独立私有运行目录，保留产品数据库、原生队列和 Worker journal 供恢复。每轮的输入、精确代码/注入 SHA、资源限制与停止规则另行冻结。

## 加载与匹配

在**隔离平台内被测的 Control 或 Worker 进程**设置以下变量，并将这三个源码目录挂载/复制到该隔离环境。不要将参数加到原开发环境或宿主工具的公共环境中：

```sh
PYTHONPATH="$PROBE_SOURCE/fault_sitecustomize:$PROBE_SOURCE:$B0_OBSERVER_SOURCE:$PYTHONPATH"
DLR_INGRESS_FAULT_ENABLED=1
DLR_INGRESS_FAULT_SCOPE=issue222-isolated
DLR_INGRESS_FAULT_ROLE=control
DLR_INGRESS_FAULT_ARM="$FAULT_PRIVATE_DIRECTORY/arm.json"
```

`PROBE_SOURCE` 指本目录；B0 观察目录只在同时启用 B0 观测时需要。新 `sitecustomize` 会先按原 `DLR_B0_ROLE` 和 `DLR_B0_DIAGNOSTICS_DIR` 安装冻结观察器，再安装故障 hook；未设置故障开关时不导入 `fault_probe`。手工入口是 `import fault_probe; fault_probe.install()`，同样必须满足全部运行环境开关。`install()` 不启动工作负载或终止进程，只有符合 arm 的真实边界才触发。

arm 路径必须绝对；父目录属于运行进程 UID 且权限 `0700`，arm 为同 UID 的普通文件、权限 `0600`，最大 8 KiB；不跟随末级软链接、不接受 FIFO、重复或未知字段。通过同目录临时文件原子替换 arm，不能在进程读到一半时原地改写。

```json
{
  "schema": "dlr.ingress-fault-arm/v1",
  "arm_id": "unique-round-id",
  "point": "t1_after_commit",
  "role": "control",
  "match": {
    "subscription_id": 123,
    "run_id": "isolated-fault-run",
    "message_id": "isolated-fault-run:000001"
  },
  "result_proof_mode": "reference-single-v1"
}
```

示例 ID 是占位值。必须指定 `run_id`，并指定 `subscription_id` 或 `execution_id` 至少一项；所有给定条件同时匹配。可追加 `attempt_id`、`receipt_id`、`message_id`。T1 尚未有 Execution，不能依靠未知 Execution ID 匹配。每轮使用全新 arm_id；同 arm_id 的完成 marker 存在后，重启仍不会再次故障注入，不删除该 marker 来循环杀进程。

## 五类切点及证明

| point | role | 精确边界与证据 |
| --- | --- | --- |
| `t1_before_commit` | control | `receive_event` 新责任返回后，捕获同一 Session；仅在其真实 `Session.commit` 调用前退出。该事务尚未提交，来源 ACK/HTTP 响应未发生。rollback/close 清除关联，重复读回不触发。 |
| `t1_after_commit` | control | 同一 Session 的原 `commit` 成功返回后、调用方继续执行前退出。提交失败不会被当成功；RabbitMQ `take_custody` 尚未返回、HTTP `_run_sync` 尚未返回，因此还没 ACK/响应。 |
| `outbox_after_confirm` | control | 仅在已检查的 Pika **1.3.2** 路径中，实际观察到 `Basic.Ack` 回调且 `basic_publish` 成功返回、confirm/mandatory/persistent 均满足时退出，位于 `mark_outbox_published` 之前。Nack、无确认返回或失败不算确认。 |
| `worker_after_claim_journal` | worker | 关联 `claim_v3` 的 EXECUTE 响应和同 Attempt；`write_attempt_journal` 成功后验证真实文件中的 ID，并严格 fsync 文件和父目录，再在执行用户代码前退出。普通日志写入不代替 fsync，未观察到 EXECUTE 响应不触发。 |
| `target_after_commit_before_result` | worker | `ControlClient.result_attempt` 入口、原网络方法调用前；要求成功 report 与同 Attempt 的真实 journal。写入并 fsync 单独诊断 receipt 后退出，原结果 RPC 尚未发送。该 receipt **只作证据**，不参与产品恢复，不冒称产品有 result journal。 |

Outbox 的来源/run 关联来自同一 Control 进程中本轮 `create_dispatch_outbox` 所见的 Execution 已加载输入。须在新轮次 T2 之前 arm；跨进程或已有历史 Outbox 缺此关联时不会猜测身份，缺 marker 不能计通过。

当前参考 Adapter 单条返回仍匹配默认 `reference-single-v1`：`run_id`、`message_id`、`payload_sha256`、`target_outcome=inserted|duplicate` 及 `timing.target_commit_returned_monotonic_ns`；只有未截断成功 report 才可切断。但 **这些用户代码数据不能单独证明外部提交**。marker 固定登记 `target_commit_verified=false`、`independent_target_oracle_required=true`，必须用独立 PostgreSQL oracle 读回原稳定键、hash 和唯一有效写入，再联合平台责任判定切点有效。目标事务内的 recorded 时间也不等于 commit 返回时间。

业务批次第五点使用 `result_proof_mode=reference-batch-v1`。探针从 Control 创建 Outbox 时看到的冻结 `Execution.input`、或真实 EXECUTE Claim 中的同一冻结输入解析身份：要求 exact `dlr.event.batch.v1`、最多 100 成员、完整输入最多 256 KiB、成员 ID/顺序唯一且每个 HTTP/RabbitMQ Envelope 都属于同一 subscription、run、source。匹配任选实际成员的 `message_id`，无需该成员是第一条；不会信任输出自述的 run/source。严格结果必须 exact `dlr.event.batch.result.v1`，batch/manifest/逐成员四个 ID 和顺序全部匹配、全部 `succeeded` 且 `error_code=null`、未截断、总大小与单成员大小符合产品合同。部分失败、unknown、not_run、缺成员、错序或错误 ID 均不算“本批业务成功返回前”切点。

可在派发前使用以下 arm，无需预先猜 Execution ID：

```json
{
  "schema": "dlr.ingress-fault-arm/v1",
  "arm_id": "unique-batch-result-round",
  "point": "target_after_commit_before_result",
  "role": "worker",
  "match": {"subscription_id": 123, "run_id": "isolated-fault-run"},
  "result_proof_mode": "reference-batch-v1"
}
```

marker 会记录由冻结输入解析出的实际 Execution、Attempt、batch、manifest hash、成员数及来源类型；输入/结果仅保留 SHA，不写入成员原文。批次 strict 输出没有单条 Adapter 的 commit timing 字段，故 `adapter_commit_marker_present=false`，`batch_result_validated=true` 只证明结果形状和输入绑定，仍必须独立目标 oracle。20 条故障轮次可能形成多个小批次；切断时必须按实际 manifest 的 N 个成员核对已提交事实，重启后再核对完整 20 条，不假设每次 Invocation 恰好含 20 条。

其他输出可用 `result_proof_mode=external-oracle`。对可解析的单条或批次输入，同样可以按 subscription+run 提前 arm；若输入无法解析，必须显式指定具体 `execution_id` 和 `run_id` 才允许后备匹配，并登记 `explicit_execution_scope_requires_ledger_oracle`。后备模式必须另用平台账本验证 Execution 与来源/run 的关系，不能只信 arm 自述。所有模式均不宣称目标已经提交或存在产品 result journal。

当前真实路径已逐一核对：HTTP `_run_sync` 和队列 `take_custody` 仍在 `receive_event` 后显式调用同 Session 的 `commit`；`per_event`/`business_batch` 只影响后续 T2，不改变 T1 hook。当前 Outbox 缓存 publisher 仍经 `_publish_row` 调用 Pika 1.3.2 的确认发布；Worker 仍在 EXECUTE 后先写真实 Attempt journal，执行结束后通过 `ControlClient.result_attempt` 发结果。版本或路径变化必须重新检查，未安装/未匹配不能当作实际切点。

## marker 与判定

触发时在 arm 同目录以 `O_EXCL` 创建 `fault-{arm_id}.json`，完整写入并 fsync 文件和目录后，调用**本进程** `os._exit(137)`；不接受外部 PID、shell 命令或 kill 目标。marker 只保留点名、PID、monotonic、白名单 ID、安装 hook、源码 SHA 与固定证明字段，不保存原文、token、DSN、SQL 或异常全文。

第五点另有 `fault-{arm_id}.result-evidence.json`，`authority=evidence_only_not_product_recovery`；它只保存白名单证明，并在主 marker 中记录其 SHA。产品 claim/cleanup journal 内容保持原样。正常重启恢复仍依靠原数据库、租约/fencing 和产品 journal；不会读取该诊断 receipt。

无 marker、marker 不完整、身份未关联、fsync 失败或源码/路径条件不符均为缺少故障证据，不能算切点通过，也不因此声称产品失败。marker 只能证明达到切点，不能代替来源重投、120/600 秒恢复、逐条内容、配额释放及独立目标 oracle 的验收。

子进程单测使用本地 SQLite 实际事务、真实 Pika 方法配模拟 Ack/Nack、真实 Worker journal 文件和模拟 Control HTTP 响应，只验证探针机制；未连接 PostgreSQL/RabbitMQ 等外部服务，不代替 B4 的真实 20 条轮次：

```sh
uv run --project backend python tools/ingress/test_fault_probe.py
```
