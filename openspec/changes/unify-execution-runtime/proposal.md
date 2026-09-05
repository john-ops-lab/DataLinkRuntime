## Why

系统刚对外发布且没有历史用户和数据兼容需求，但全新安装仍默认走旧任务领取流程，导致已实现的可靠执行与资源隔离没有成为实际运行基线。根据本次明确授权，将新机制收敛为唯一机制，并去除用户需要理解的版本和切换概念。

## What Changes

- **BREAKING**：Task、Schedule、Webhook 统一通过 RabbitMQ、Outbox、Claim、Attempt/Slot、Lease/Fencing 和资源 Sandbox 执行，删除旧领取和执行回退分支。
- **BREAKING**：移除 legacy、canary、minimum protocol 和人工 Cutover attestation 配置；默认部署提供可验证的 Linux private-cgroup Sandbox。
- 界面与当前安装文档使用“运行节点”“执行能力”“资源隔离”等产品语言，不再展示 V3 标识；内部协议编号可以保留。
- 保留真实 readiness 校验，未通过隔离检查的 Worker 不可执行任务，不能仅修改状态显示。
- 重建本次独立测试环境并验证真实新链路，保留其他项目资源以及当前模板广场改动。

## Capabilities

### New Capabilities

- `unified-execution-runtime`：唯一执行链路、默认部署、能力门禁和不带版本的用户体验。

### Modified Capabilities

无；历史 Issue #130 change 和历史 milestone 文档保留为历史记录，本变更明确取代其旧机制兼容和分阶段切换假设。

## Impact

影响 Control 服务/API/config、Worker、数据库新安装约束、Compose/启动脚本、系统状态界面、测试与当前部署文档。不升级 Ant Design，不改变模板广场契约，不实现 HA 或不可信多租户隔离。

不提供旧二进制和旧执行数据的在线迁移兼容。本次测试数据库可重建，但必须先确认其归属及内容；其他数据库、卷和容器不在范围内。历史 Alembic 链保留，用新迁移收敛现行约束。回滚是重新部署对应版本的干净环境，而非将新行交给旧执行器。Git 提交、PR、发布和用户验收不在自动完成范围。
