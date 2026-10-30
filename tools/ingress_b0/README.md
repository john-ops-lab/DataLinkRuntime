# B0 测量夹具

本目录只用于 [Issue #222 的 B0](../../openspec/changes/add-bounded-durable-ingress/b0-measurement-plan.md)，不注册产品入口、不优化调度、不代替真实 Worker 或容量验收。地址、凭据、运行清单和原始证据保存在仓库外；不要把私有配置样例替换成真实配置提交。

- `workload.py`：生成固定输入、准备独立目标表、真实异步 confirms 发布、独立目标对账。正式 `count=200,warmup=false`，预热 `count=20,warmup=true`；原消息 ID/正文跨重投保持。
- `adapter.py`：通过普通版本 API 保存的 Python Adapter 源码；显式依赖 `psycopg[binary]==3.3.4`，凭据绑定名 `TARGET_DSN`，每次一个连接、一个事务，无自动目标重试。
- `sitecustomize.py` / `observe.py`：仅当隔离进程显式设置 `DLR_B0_ROLE=control|worker` 及 `DLR_B0_DIAGNOSTICS_DIR` 才安装懒加载观测；注入目录加入该进程的 `PYTHONPATH`。未启用不导入产品。原应用异常、返回、事务和等待保留；日志不保存正文、SQL/参数或凭据。
- `collect.py`：在隔离 Linux VM 采集真实 cgroup/进程资源，在独立只读进程采集既有账本计数；每秒采样、保留耗时与缺口。采集器须检查终态及样本尺寸，不能只看启动成功。

`workload.py` 从 `--config` 读取 JSON：`run_id`、`count`、`warmup`、`input_path`、`output_path`、`target_dsn`、`amqp_url`、`queue`。前两个路径指向运行进程所见的仓库外位置，`output_path` 是目录。证据文件排他创建；失败后不可覆盖或只换 run_id 重发到原 epoch。

```sh
python3 tools/ingress_b0/workload.py generate --config "$B0_PRIVATE_CONFIG"
python3 tools/ingress_b0/workload.py prepare-target --config "$B0_PRIVATE_CONFIG"
python3 tools/ingress_b0/workload.py publish --config "$B0_PRIVATE_CONFIG"
python3 tools/ingress_b0/workload.py verify --config "$B0_PRIVATE_CONFIG"
```

资源采样配置是 `{"cgroups":{"control":"/实际组路径","worker":"/实际组路径"},"disk_path":"/var/lib/docker"}`，以实际所有平台组件补齐。可选 `clock_host`/`clock_port` 指向只返回 UTC 纳秒并以换行结束的外部夹具，记录偏差与 RTT 误差界。账本采样只需 `adapter_id`，由隔离 Control 的环境取得数据库配置，显式关闭诊断，避免采样进程的查询混入产品诊断。

```sh
python3 tools/ingress_b0/collect.py resources --config "$B0_RESOURCE_CONFIG" --output "$B0_RESOURCE_LOG" --seconds 920
python3 tools/ingress_b0/collect.py ledger --config "$B0_LEDGER_CONFIG" --output "$B0_LEDGER_LOG" --seconds 920
python3 -m unittest tools.ingress_b0.test_workload
python3 tools/ingress_b0/test_observe.py
```

两份诊断角色日志各限 12 MiB；资源采样限 6 MiB；账本采样限 1 MiB；预热与正式发布日志各限 512 KiB。重复阻塞扫描只保留前两次明细，全部调用另有精确聚合；不能用采样 SQL 明细计算全体分位数。停止隔离进程前先确认责任已结清，以正常退出取得最后聚合。Hook 缺失、预算耗尽、采样失败和时钟偏差须如实报告。

目标 oracle 只证明目标事实；Broker confirm、DLR 账本、执行/清理责任和目标结果须独立对账。目标事务内时间戳不等同 commit 返回时刻；参考 Adapter 单独返回驱动导入、连接与 commit 的实际计时。B0 通过标准及停止规则以测量计划为准。
