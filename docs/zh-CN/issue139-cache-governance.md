# Issue #139 Worker 缓存治理运维说明

本文说明 Worker 版本缓存治理的配置、安全边界和恢复原则。它描述最终管理合同；管理页面和命令只有在对应版本已部署并通过验收后才可使用。构建成功或代码存在不等于当前环境已经启用缓存回收。

## 默认行为

周期回收和容量压力回收默认都关闭。Worker 仍可复用已有的五语言版本缓存，并可生成只读统计；只有显式启用相应开关后，才会开始新的自动回收轮次。

所有五语言依赖准备、统计、旧清理和恢复使用同一份 Worker 生效策略与同一个版本缓存总预算。版本缓存的实际占用按文件系统中的完整持久树计算，包括 manifest、ready 标记、处理中 trash 和不再由有效 reservation 覆盖的 staging；不能用 manifest 中的逻辑内容字节代替总占用。

| Worker 环境变量 | 默认值 | 允许范围与含义 |
| --- | ---: | --- |
| `DLR_CACHE_GC_ENABLED` | `false` | 周期回收开关，严格布尔值 |
| `DLR_CACHE_PRESSURE_GC_ENABLED` | `false` | 高水位、磁盘余量或安装预留不足时的容量回收开关，严格布尔值 |
| `DLR_CACHE_SCAN_INTERVAL_SECONDS` | `300` | `10`–`86400` 秒 |
| `DLR_CACHE_IDLE_TTL_SECONDS` | `2592000` | `60`–`31536000` 秒；周期候选的闲置下限 |
| `DLR_CACHE_MIN_IDLE_SECONDS` | `86400` | `0`–`IDLE_TTL_SECONDS`；容量和人工清理也必须保留近期项 |
| `DLR_CACHE_MAX_BYTES` | `4294967296` | 正整数；版本缓存总预算 |
| `DLR_CACHE_HIGH_WATERMARK_PERCENT` | `85` | 高水位；必须满足 `0 < LOW < HIGH < 100` |
| `DLR_CACHE_LOW_WATERMARK_PERCENT` | `70` | 容量回收目标水位 |
| `DLR_CACHE_DISK_RESERVE_BYTES` | `134217728` | 非负且小于 `MAX_BYTES`；必须留给文件系统的空闲量 |
| `DLR_CACHE_MAX_DELETE_BYTES_PER_ROUND` | `268435456` | 正整数且不大于 `MAX_BYTES`；不足以开始一个完整项时保留该项 |
| `DLR_CACHE_MAX_DELETE_ENTRIES_PER_ROUND` | `20` | `1`–`1000` |
| `DLR_CACHE_MAX_SCAN_ENTRIES_PER_ROUND` | `200` | `1`–`10000`；使用稳定游标继续后续轮次 |
| `DLR_CACHE_MAX_SCAN_NODES_PER_ROUND` | `100000` | `1`–`1000000`；单项递归也消耗此预算 |
| `DLR_CACHE_MAX_SCAN_HASH_BYTES_PER_ROUND` | `268435456` | 正整数；未完成 hash 的项保持未知 |
| `DLR_CACHE_MAX_SCAN_DEPTH` | `64` | `1`–`256`；不跟随目录软链接 |
| `DLR_CACHE_MAX_ROUND_SECONDS` | `10` | `1`–`60` 秒；到期后不开始新对象 |
| `DLR_CACHE_STAGING_TTL_SECONDS` | `86400` | `60`–`31536000` 秒；超过 TTL 仍需满足其他全部保护条件 |
| `DLR_CACHE_OFFLINE_PROTECTION` | `true` | 默认启用来源与离线保护；关闭也不能绕过有效重建证明要求 |
| `DLR_CACHE_OFFLINE_MODE` | `false` | 显式离线部署；启用保护时只接受 `verified_offline` 或平台内置材料证明 |
| `DLR_CACHE_SHARED_CACHE_MODE` | `report_only` | 本轮唯一允许值；共享下载缓存只统计、不删除 |

配置在 Worker 启动时严格校验。错误布尔值、越界值、低水位不小于高水位、磁盘保留不小于总预算等配置会拒绝启动，不会静默变成无限制或退回另一组值。修改环境配置后，应在正常变更窗口重启 Worker，并从 Worker 缓存入口核对实际生效策略；不要只检查配置文件文本。

## 允许成为候选之前

一个版本环境即使已经闲置，也默认是 `rebuildability=unknown`，不能回收。以下条件同时成立时才可能成为候选：

- 当前内容 identity 和 digest 与确认完全一致；
- 未 pin，且已经超过对应的近期保留时间；
- 存在仍有效的重建确认，最长有效期为确认后 24 小时；
- 没有 Execution、Attempt、Slot、cleanup、Incident、恢复材料、持久 use 或 journal 责任；
- Worker root、对象身份、Control guard 和本地文件事实全部可核对；
- 在真正 rename 前再次检查上述条件。

pin 始终优先。内容、identity 或 digest 变化会使旧确认失效，历史安装成功、固定版本字符串或 URL 曾经可达都不构成证明。

重建确认有两种明确语义：

- `verified_offline`：管理员确认当前有不可变、可用并覆盖该环境全部依赖的本地材料；显式离线模式下仍可使用。
- `managed_online`：管理员确认当前有受控、可重复且可用的在线来源。这是限时运维承诺，不表示平台主动验证了外部仓库；显式离线模式下会被保护。

确认必须绑定当前 identity/digest，包含非敏感说明、操作者、确认时间和到期时间。只有无外部依赖且平台已经核验内置材料 identity 的环境，系统才可自动写入 `verified_offline` 确认。

依赖准备若以稳定结果报告同一来源未配置或不可用，使用该来源的旧环境会被保留。来源关系只保存去除凭据后的稳定私有标识，不保存 URL、Token 或日志正文。管理员核对材料或来源后，需要对当前 identity/digest 重新确认，才能解除这项保护。

## 分类和容量策略

Worker 缓存入口按以下类别报告实际占用：

- **版本环境**：可验证的 Adapter 版本缓存；只有完整通过保护检查的项才计为预计可回收。
- **共享下载缓存**：uv、npm、Maven、Go 等共享根，仅报告 `shared_cache_not_supported`，不会随某个 Adapter 清理而删除。
- **失败 staging**：只有名称和归属可验证、reservation 已失活、超过 TTL，且无进程、use、journal 或恢复操作时才可按预算清理。
- **处理中 trash**：属于已有持久操作，继续计入实际占用并按原 operation/generation 向前恢复。
- **未知目录或旧布局**：报告 `cache_ownership_unknown` 并保留，不根据名称猜测归属。

周期回收只选择超过 `IDLE_TTL_SECONDS` 的安全项。容量和人工清理还要遵守 `MIN_IDLE_SECONDS`，并按最后使用时间最旧优先；时间相同时先考虑较大项，再按 key 稳定排序。安装预留不足触发的容量回收在腾够本次预留所需空间后停止；后台压力回收在占用降至低水位且磁盘安全余量恢复后停止。所有轮次仍受原预算与保护条件限制。

每次安装预留失败最多执行一个有界压力回收轮次，然后只重试一次预留。没有安全候选、回收后仍不足或单项超过本轮预算时，应分别显示稳定原因，不得循环强删。周期、压力和人工操作共用单 Worker 轮次锁；版本 key 锁使用非阻塞竞争，正在准备或使用的缓存不会因锁等待形成互锁。

## 管理入口合同

最终管理入口位于 Worker 的缓存区域。页面应显示采样时间、是否完整、实际策略、各类占用、预计可回收量和保留原因。离线、过期或分页未完成的快照只是观察信息，不能作为删除许可。

管理员操作遵循以下合同：

- 预览只反映采样时刻；执行前会重新检查全部 Control 和 Worker 事实。
- 同一 Worker 同时只允许一个活动管理操作；重复幂等请求返回原操作，不叠加清理。
- 一次操作最多指定 200 个 key；选择部分 key 时不得清理未选择项。
- protect 必须绑定最近看到的 identity/digest；冲突时拒绝，不能覆盖新内容。
- 操作和审计记录只保存非敏感 ID、稳定原因、估算与实际释放字节、operation/generation 和时间。
- 查询分页最多 100 条；终态审计默认保留 90 天且每 Worker 最多 1000 条。未完成操作、guard 或仍被 cleanup 引用的审计不能按保留期删除。

失败的 Adapter cleanup 通过原 `cleanup_id` 重试，不创建第二条清理通道。示例：请求在 `failed/3` 时被管理员重试，先成为 `pending/3`，下一次真实领取成为 `running/4`；如果再次失败，下一次新的管理员重试才会产生第 5 次真实领取。每次管理员重试只增加一次逻辑尝试，预算续作或 Worker 重启仍属于同一次尝试，旧 claim 回报不能完成新领取，attempts 不重置。

管理清理失败后，原失败审计仍保留。后续显式重试有自己的操作记录，并汇总原清理中已经完成的子项，实际释放空间不会重复计算。目标已经恢复完成时，新的重试请求会被拒绝；重复发送同一幂等请求仍返回原操作。

## 断联、故障和恢复

无法核对 Control、journal、use、root owner、identity、digest、来源或恢复责任时一律保留。未知 journal、软链接、损坏 sidecar、开放 Incident、deferred cleanup 和活动 Attempt 都不能被人工操作或配置开关绕过。

删除使用持久 guard、operation/generation、原子 rename 到私有 trash 和有界续作。rename 之后的中断必须由原操作向前完成或安全停止，不能把 trash 当成新候选，也不能用新的终态回执释放更新后的 generation。连续三次真实失败后停止自动热重试，等待明确的管理员重试；重试仍执行全部保护检查。

关闭 `GC_ENABLED` 或 `PRESSURE_GC_ENABLED` 只停止新的对应自动回收轮次，不撤销已经取得 guard 或已经 rename 的事务。恢复程序仍须收敛这些持久操作，否则会丢失已发生文件系统动作的责任。

不得通过清空 Worker 卷、删除 lifecycle/journal、手工移动缓存目录、伪造回执或绕过 guard 来“修复”空间。存在活动缓存责任或未完成治理操作时也不得降级到无法识别这些事实的版本。升级或恢复应保留 Worker runtime/journal 卷，并先完成当前 operation/generation 的安全向前恢复。

## 启用前核对

在实际环境启用自动回收前，至少确认：

1. 当前部署版本包含完整的 guard、use/journal、恢复和策略实现，而不只是配置项。
2. Worker 缓存入口显示的实际策略与计划一致，默认关闭状态没有被误改。
3. 待回收项拥有当前、限时且覆盖完整依赖的重建确认；共享和未知占用不计入可回收量。
4. 使用专用测试环境验证低容量安装、并发使用、断联、重启和部分 trash 恢复；构建或单元测试不替代运行验收。
5. 先使用保守预算和阈值观察，再按实际占用调整；不要把缩小预算当成强制删除方式。
