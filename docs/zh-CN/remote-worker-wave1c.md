# Wave 1-C：完整停止检查点下的未提交结果结清

W1-C 在 [W1-B](remote-worker-wave1b.md) 的同宿主、同 boot、原身份和原卷恢复流程中，增加结果未提交时的显式处置。仅支持单 Control、单固定 Worker、标准库 Python、none/JSON 输入、手动或定时任务；#129 的其余 Wave、Pool 和 HA 保持开放。

**平台未确认业务结果，外部目标可能已经产生效果；此操作保留现有失败/取消记录，只结清本节点清理责任，不重试业务，也不回滚外部效果。** 管理员确认的是这一处置意图，不能据此宣称业务成功、业务失败或外部效果已撤销。完成情况以阶段 PR 的精确 SHA、镜像和真实验收记录为准。

## 资格与取消顺序

必须有 W1-B 或更新版本在业务放行前真实生成的完整 prepared，以及真实监督链在结果 finalize 前生成的 stopped；两者绑定原对象、两份冻结额度和匹配的 staged result intent/MAC。缺失、损坏、Claim 未知、没有 stopped、宿主重启、复制卷或跨宿主接管继续阻断，不补造记录。

| 权威数据库状态 | 操作 |
| --- | --- |
| 匹配 result finalization 已提交 | 仅按 B 资格处理；C 拒绝。B 的取消冲突仍阻断 |
| Attempt `worker_lost`，Execution `dead_letter` | 可以明确保留失败和未知业务结果，登记 C |
| Attempt `worker_lost` 或 `cancelled`，Execution `cancelled` | 可以登记 C；保留原 Attempt 状态 |
| Execution `running` | 调用原 `POST /api/executions/{id}/cancel`，等待原租约 reconciler 提交允许终态 |
| Execution `retry_wait` | 先调用原 cancel，读回 `cancelled`；旧 cleanup 未结屏障持续阻止重试派发 |
| 新 Attempt/dispatch 已出现、部分 finalization、身份或记录冲突 | 拒绝，保留原责任 |

不要先清理再等待取消。C 不创建 Execution/Attempt，不增长 attempt_count/dispatch，不调用 Replay。dead-letter Hold 保留原生命周期；未来人工 Replay 是独立的新责任，须另行判断业务幂等和对账条件。维护期间定时后继 Execution 也是独立任务，只有显式激活后才可领取。

## 升级与原宿主核验

先按 B 说明 drain，保留数据库、原 runtime volume、journal bind、稳定身份、凭据和密钥；停写后应用前向迁移 `0087_issue129_disposition`，部署匹配的 Control/Worker 镜像。运行角色须按 [W1-A 权限步骤](remote-worker-wave1a.md) 撤销 `worker_maintenance_operations` 的 DELETE 权限；角色不能修改审计保护或事务触发器。启用门禁会检查这些事实。

迁移仅给维护审计添加可空 disposition ID、完整事务号和封闭快照；旧 B 行保持为空，不改写旧业务字段。存在 C 处置时，数据库拒绝降级到 B；本地 terminal v2 也不能交给只理解 v1 的 Worker。不得以旧备份覆盖新责任、删审计/检查点或换空卷来回退。B 完整 v1 prepared/stopped 可以作为 C 输入，无需重新执行业务。

沿 B 的事前建档、整容器 stop/wait、原 cgroup/后代/IO 退出、boot、原卷独占和无未知 FD/namespace 导出步骤核验，并保存私有宿主原件。管理员证据引用/摘要只作为 `operator_attestation`，Worker 仍独立核验其可观察的对象、挂载、根锁和本地记录；摘要不能替代真实物理停止。

## 登记和重入

复用原管理员 `POST /api/workers/{id}/v4/recovery-intent`。B 请求省略 mode 时仍按 `committed_result` 处理；C 必须明确提供 `settle_stopped_uncommitted` 和完整 settlement。保留 B 的原 binding，增量如下：

```json
{
  "request_id": "NONZERO_REQUEST_UUID",
  "expected_version": 5,
  "binding": {"...": "使用 B 说明中的完整原责任与宿主绑定"},
  "mode": "settle_stopped_uncommitted",
  "settlement": {
    "stopped_sha256": "CANONICAL_STOPPED_SHA256",
    "result_intent_id": "ORIGINAL_STAGED_INTENT_UUID",
    "body_mac": "ORIGINAL_64_LOWERCASE_HEX_MAC",
    "attempt_status": "worker_lost",
    "execution_status": "cancelled",
    "acknowledgement": "result_uncommitted_outcome_unknown"
  }
}
```

示例 binding 省略号不能直接提交。摘要算法与 B prepared 相同：解析后按键排序、紧凑 ASCII JSON 的 SHA-256，不含末尾换行。result intent ID/MAC 来自同一 stopped，MAC 只关联原记录，不代表成功结果。expected version 使用当前管理员 status；预期终态使用已提交的实际值。

成功返回独立 `disposition_id`、显式 mode/settlement 和新 maintenance version，容量为 reconciling；不写 result finalization，也不释放 Slot、输入 Lease 或本地额度。登记响应未知时，用相同 request ID、操作者及完整参数重试，只读回同一 disposition；参数冲突拒绝。HTTP 200 也不能代替 Worker 的分离事务提交观察。

将 request ID 传入 B 已有的 `DLR_WORKER_RECOVERY_OPERATION` / `--recovery-operation`，在同一 Compose service 上重建原卷的新实例。无需新工具、额外服务或结果正文文件。Agent 在注册前只读扫描及读回精确意图，通过后注册新 generation；恢复期间不启动 consumer。C 参数或证明不能降级为 B；原运行凭据、当前实例证明、两代 history 和原 Cleanup Token 均须有效。

## 清理、查询与再运行

顺序为：新鲜的 `platform_terminal` 历史观察 → B 原物理清理及持久进度 → cleanup stage/finalize → 独立读回同一 committed completion → terminal v2 精确清单 → 逐项 unlink/fsync → 最后移除终结锚点。C 的证明和 terminal 使用 disposition ID；B 使用真实 finalization ID，二者不能互换。版本化证明绑定 kind、原/当前身份、检查点、原 staged intent/MAC、原终态、nonce 和期限。

处置、清理提交回执未知，或终结清单部分删除后退出，均保留责任。下一次显式恢复以当前新权限读回同一 disposition/completion，沿原清单继续；不重新 stage 已提交 completion，不重跑业务或重复结账。未知对象、替换、额外挂载、损坏记录和未结束 IO 仍阻断。

业务 status/output/error/ended_at、Attempt 状态/计数和 dispatch 保持原值。管理员 `GET /api/workers/{id}/v4/status` 的 `last_recovery_disposition` 保存最近 C 操作的原终态、Execution/Attempt、disposition ID 和固定原因 `result_uncommitted_outcome_unknown`；后续 drain/activate 不会将其变成业务成功。相同原 request 也可读回原处置。

两份冻结占用一直保留到本地记录和真实 IO 结束，不调用消失进程的 release，不给新预算加旧额度。全部归零后才重新核验实际 envelope，建立一次新账；随后按 B 流程确认 disabled 并显式 activate。新 channel 只 ACK 服务端权威 `ACK_NOOP` 对应的当前 delivery tag，最后实际完成一项新任务。安全阻断本身不构成 C 成功结清。
