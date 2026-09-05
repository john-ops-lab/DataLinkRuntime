# 本地验证记录

验证时间：2026-09-05 至 2026-09-06（Asia/Shanghai）。本记录针对未提交工作区，不代表 CI、PR、合并、发布或用户验收。

## 结果

- Control 普通 Task、Schedule、Webhook 统一走 RabbitMQ/Outbox/Attempt/Slot；旧执行领取、旧回报 API、旧 Worker loop、canary 与人工 Cutover 配置已移除。
- Worker 只有固定内部协议和 Sandbox 路径；界面不再展示 V3 迁移用语，真实隔离门禁保留。
- 修复回归发现的运行中日志不写入 Execution、锁等待后 ORM 缓存可能覆盖终态，以及三种调度策略丢失具体输入错误详情的问题。

## 回归与检查

| 范围 | 证据 |
| --- | --- |
| 后端完整回归首轮 | 1502 passed / 29 failed / 11 skipped；部分旧测试修改发生在该运行期间，因此首轮不是全绿结果 |
| 首轮全部失败文件复验 | API、输入与调度 97 passed；迁移、模板与扫描 21 passed；凭据与本地化 19 passed；包源 20 passed；均无剩余失败 |
| 真实 Linux Sandbox | 9 passed，使用 private cgroup namespace、真实内核限制与清理；不使用 FakeSandbox |
| 前端 | 已完成受影响组件/应用测试；最终 lint、typecheck、生产镜像构建通过 |
| 浏览器 | 模板广场 4 passed；系统状态中英文/1280–1920px/状态切换 9 passed |
| 静态与规格 | Ruff check/format、mypy 123 source files、Compose 两种配置审计、shell syntax、OpenSpec strict validation、git diff --check 通过 |

后端最终使用失败文件定向复验，未将其表述为又一次完整套件全绿。首轮跳过的 9 项内核测试已在 Linux 实测补齐；2 项专用 RabbitMQ 集成测试未在该回归命令中启用。真实 RabbitMQ 正常执行另有下述证据，不替代所有故障注入场景。

## 干净部署与真实执行

- 地址：`http://127.0.0.1:8132`；Compose 项目：`dlr-unified-test`。
- 从空数据库迁移至 `0033_unified_execution`，随后启动 Control/Worker/Web。
- Worker `worker-1` 在线且预检通过，全部 18 项能力为 true，包含 `cgroup_namespace_private` 和 `resource_envelope_verified`。
- 实际 Docker cgroup namespace 为 `private`，父级为 `/system.slice/dlr-unify-runtime-test.service`，只挂载其委派子树。
- 三语言 `json-mapping-cleaning` 模板均经公开 API 复制、保存、运行；Execution 1/2/3、Attempt 1/2/3 均使用 RabbitMQ 并成功，输出与模板示例一致。未模拟 Claim/start/result。
- 每次终态后 Slot 已释放；沙箱清理 `completed`、`residue=false`。真实限制读回：CPU `100000 100000`、memory `536870912`、swap `0`、pids `128`。
- Execution 4 的数秒日志任务运行期间，独立 GET 观察到 4 次 stdout 增长，真实 SSE 收到 4 个日志增量，接收时任务仍为 running。
- 最后一个生产修复仅涉及 Schedule 错误详情；Control 已重建并重启，运行容器与工作区 `schedule.py` 的 SHA-256 均为 `6ea965f8617a98c78ef16dc3fbc203cd4cc400271291172392cb77b3c7f5d5db`。重启后 PostgreSQL、RabbitMQ、Outbox、Worker 与全部 5 个容器健康。

## 页面验收

Browser plugin not available；使用项目已有 Playwright。真实页面流程为登录 → 系统状态 → JSON 模板 → 复制为独立 Adapter → 保存。确认页面标题、非空内容、无框架错误覆盖层、无 console/page error，复制代码在编辑器可见、保存后绑定真实运行节点。

截图位于 `/tmp/dlr-template-simplify.Z0zkWp/unified-home.png` 与 `unified-copy.png`。三语言与 SSE 详细无凭据证据位于 `/tmp/dlr-unified-api-qa-evidence.md`。未覆盖其他浏览器引擎；响应式模板测试包含窄屏。

## 清理与交付边界

- 用户明确要求删除旧库后，移除 `dlr-template-test` 的 5 个容器、5 个专属卷和网络，包括旧 Adapter `123`、`675`；无备份，未触碰其他项目。
- 新环境所有本次试跑 Adapter、Version、Execution 已清理；最终数量均为 0。5 个 Worker cleanup 请求均 completed。
- 保留新环境服务、数据卷、委派 unit 和启动所需本地配置，供用户试用。该 transient unit 在 Docker VM 重启后需重新准备。
- 未进行 Git commit、PR、合并或发布，等待用户验收。
