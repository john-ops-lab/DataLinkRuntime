## Context

动机见 proposal.md。当前可靠执行实现与 legacy 领取实现并存；配置、Worker 默认协议和普通入口仍以旧机制为默认值。现有 Sandbox 已提供能力探测，Compose 示例的 host cgroup namespace 与当前可靠性文档不一致。本次授权明确不保留旧机制兼容。

## Goals / Non-Goals

**Goals:**
- 复用现有可靠执行服务、Consumer、Sandbox 和恢复逻辑，删除选择旧路径的代码。
- 默认配置固定唯一内部协议和消息执行后端；配置错误与资源隔离失败必须显式失败。
- 本地测试环境从新数据库迁移开始，记录真实 Worker 能力、消息与执行证据。

**Non-Goals:**
- 不为旧数据设计在线迁移，不重写历史迁移链，不把历史规格文件批量改成当前说明。
- 不改变编程语言、前端框架、凭据合同与已完成的模板广场行为。
- 不将代码修改、测试通过与用户验收/提交/发布混为一谈。

## Decisions

1. 普通执行直接调用现有可靠执行 Admission。移除 legacy Claim、旧执行回报与 Worker 旧 loop；保留 Consumer 使用的内部协议编号 3，不为消除 UI 标识重命名整个 wire contract。相比只修改默认开关，这能避免配置误用后回退旧机制。
2. 保留 Attempt/Slot、journal、Outbox、租约、Fencing 与所有真实 Sandbox 能力检查；删除仅为过渡期存在的 canary、legacy 和人工切换配置。新迁移收敛执行 backend 默认值、约束与旧索引，不伪造迁移验收。
3. Compose 使用 private cgroup namespace 与明确委派目录，启动脚本以实际探测为准。不能将 host namespace 或手填 capability 当作成功证明；缺少宿主支持时报出具体前置条件。
4. UI 只调整产品术语及真实 readiness 数据依赖，不移除隔离门禁。README、产品/架构和安装说明同步唯一机制，历史 Cutover 文档标明历史用途。
5. 分别验证服务/配置合同、Worker 与 Sandbox、页面状态和全新 Compose 真实执行；隔离资源和数据库均使用本次任务标识，避免影响其他项目。

## Risks / Trade-offs

- [移除旧机制会使部分现有测试夹具失效] → 将当前行为测试迁至唯一机制；仅删除只验证已取消合同的测试，不以跳过失败替代修复。
- [macOS Docker VM 的 cgroup 委派与宿主 Linux 存在差异] → 在实际 VM 验证 private namespace、挂载与能力，使用相同 Sandbox 检查，不放宽必需能力。
- [大范围改动与已有模板改动共存] → 文件责任明确，保持现有 dirty diff，运行受影响回归。

## Migration Plan

1. 完成唯一机制代码和新数据库迁移，更新默认部署说明。
2. 对本次 dlr-template-test 项目进行只读盘点，确认无用户新增业务数据后重建任务专用数据库与服务；保留其他项目资源。
3. 迁移完成后启动 Control、通过真实隔离预检的 Worker 与 Web，验证三语言执行、日志、终态与系统状态。
4. 向用户交付测试地址和证据；不提交、PR 或发布。若回退测试版本，重新创建该版本干净环境，不使用旧执行器解释新数据。
