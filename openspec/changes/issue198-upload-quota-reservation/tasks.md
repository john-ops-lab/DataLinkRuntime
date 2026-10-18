# Tasks

## 1. 原子预留修复与边界验证

- [x] 1.1 实现初始/扩容有界预留，覆盖真实 PostgreSQL/API 净 1MiB、剩余恰好/多 1B、单文件限制组合与小余量，并记录结果。
- [x] 1.2 覆盖同 Adapter 与跨 Adapter 平台并发竞争、失败/删除回收和批量效率，运行新回归及既有上传 hardening/lifecycle tests。
- [x] 1.3 更新验证说明，运行 Ruff、mypy、OpenSpec strict 校验并记录未做浏览器验收的边界。

## Workflow follow-up

- 主 Agent 统一建立 PR、选择固定本地验收入口并执行真实 Chrome 上传/保存/大小/hash 验收；本工作树不自行部署、push 或建 PR。

## Validation Result

2026-10-14：原始 HEAD 红测复现 HTTP 409；修复后 97 项真实 PostgreSQL/API 和上传生命周期回归通过，其中新增 23 项。Ruff、format、mypy（176 source files）、OpenSpec strict 与 git diff --check 通过。真实 Chrome 上传与固定入口部署由主流程完成，本候选未执行。
