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
4. 原始测试交付已完成；后续按 Issue #144 追加修复到 PR #143，通过最新 HEAD 及合并后 main CI，再在同一 main SHA 创建 v0.4.1 标签与 Release。

## Issue #144: nsdelegate 拓扑修复

CI 在 `018698c` 证伪了将 Attempt 放到 Docker namespace 根的兄弟组的部署假设。祖先 bind 可见不等于允许迁移；启用 `nsdelegate` 时迁移两端必须在 namespace 内。原本机 Colima 证据未启用此选项，保留为历史记录，不作为新版本 Linux 验收。

- 宿主 P 保留委派及外层预算，Docker 为每个容器 C 设置有限 CPU、memory、swap、pids；C 内分为 `agent` 与 `attempt-*`。初始化验证祖先 bind 的真实 mount root `/..`、C 与 canonical namespace 根的设备/inode 相同，然后将 C bind 到管理路径，隐藏原祖先视图。只在 C 内移动启动进程并启用子组控制器，不写 C 的资源上限。
- 启动前读回 C、P 及宿主准备脚本验证的有效祖先额度。每个 Worker 使用自身 C 额度计算 Slots 和管理进程预留；共享 P 下的容器额度合计不得超过 P。独立实例必须使用唯一 Worker 名称、runtime/journal/log 目录；本次双实例回归不代表 HA。
- 最终拓扑验证要求管理进程在 `/agent`，管理挂载 root 为 `/` 且与 canonical cgroup 根身份一致；拒绝旧祖先布局。Docker exec、healthcheck、停止、重启、故障清理和恢复均需要真实验收。
- 恢复记录必须绑定实际 cgroup 身份；namespace 重建后不得按旧名称清理当前实例的同名对象。只有证明旧对象已不存在才可结束旧记录，否则保留诊断，不能虚报清理成功。清理失败保留主错误及 errno。
- 不修改模板目录、草稿复制/首次保存合同、语言和登录入口；不恢复旧执行路径，不引入 HA、节点身份重构或业务幂等功能。五个最简常驻容器的边界不变。
