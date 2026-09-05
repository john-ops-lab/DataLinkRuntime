## Purpose

为全新安装提供唯一且可验证的可靠执行机制，让管理员无需理解旧协议兼容、灰度和切换步骤即可运行适配器，同时保证资源隔离、串行执行权威、失败恢复与系统状态展示一致。

## ADDED Requirements

### Requirement: 唯一任务执行机制

系统 SHALL 将 Task、Schedule、Webhook 的每一次新执行通过持久消息分发、执行尝试与租约隔离进行处理；MUST NOT 提供旧任务轮询领取或失败回退至无隔离执行的路径。同一 Adapter 同时最多运行一个 Attempt，排队执行固定不可变输入和代码快照。

#### Scenario: 全新安装运行任务
- **WHEN** 管理员在默认安装中保存并运行适配器
- **THEN** 执行产生 RabbitMQ backend、Attempt 与 Slot 记录，并通过资源 Sandbox 完成，无需开启灰度或切换开关

#### Scenario: 旧节点与领取请求
- **WHEN** 旧 Worker 协议注册或调用旧领取接口
- **THEN** 系统拒绝其参与执行，且不创建旧机制执行记录

### Requirement: 执行能力以真实资源隔离为前提

系统 SHALL 在 Worker 启动时验证 Linux 私有 cgroup namespace、可写且正确委派的 cgroup v2 和现有 Sandbox 必需能力；MUST 在隔离失败时保持不可执行，而非修改状态标识绕过检查。

#### Scenario: 隔离不可用
- **WHEN** 节点未通过必需隔离检查
- **THEN** 节点不接收用户任务，系统状态明确显示运行能力不足

#### Scenario: 合法隔离节点
- **WHEN** 节点通过资源隔离预检并在线
- **THEN** 系统状态承认其真实执行能力，适配器进程在其受限子 cgroup 中运行

### Requirement: 默认部署无需历史切换流程

系统 SHALL 为全新数据库和受支持 Linux Docker 环境提供唯一执行机制的配置与启动说明；MUST NOT 要求用户填写旧机制开关、协议选择、canary 或人工 Cutover attestation。

#### Scenario: 从示例配置启动
- **WHEN** 用户按当前安装文档准备必要连接凭据并部署
- **THEN** Control 和 Worker 使用唯一机制，启动时给出真实能力检查结果，历史迁移流程不成为安装步骤

### Requirement: 产品界面不展示内部执行版本

系统 SHALL 使用执行能力和资源隔离描述节点状态及故障，不在用户操作入口与提示中展示 V3 或要求启用某个旧新机制版本；内部协议编号和机器字段可以保留。

#### Scenario: 查看系统状态
- **WHEN** 管理员查看节点状态或执行不可用提示
- **THEN** 页面解释连接或资源隔离问题，不出现“启用 v3”等迁移术语
