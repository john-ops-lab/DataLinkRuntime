# Worker Pool Routing Specification

## Purpose

定义停止态 Worker Pool 路由管理、固定节点兼容、运行快照及生产接收边界，使管理员能够审计规则与成员的变化，并让各接受和补处理入口沿用其原有的权限、版本及幂等约束。

## Requirements

### Requirement: 绑定、规则版本与管理授权
运行绑定 SHALL 使用 schema 1，fixed 绑定稳定 Worker 并保持 strict fixed；pool 引用 Pool 及不可变 revision。规则变化生成新 revision，已引用 revision MUST 保留。候选规则仅使用管理员授予的成员、标签键值等值合取和 placement scope，并核对冻结环境、材料及凭据授权。授权变动作用于新的资格判断。

#### Scenario: 标签相同但环境或授权不符
- **WHEN** 节点自报所需标签，或仅语言相同但镜像、架构、工具链、材料能力或管理员授权不符
- **THEN** 节点不具备该规则的候选资格；未知历史环境保持原 fixed 绑定

#### Scenario: 持有旧路由版本的配置更新
- **WHEN** 客户端用过期配置版本、运行锁冲突的请求，或不一致的显式 route 与 runtime_worker_id 写入
- **THEN** 维持原配置，字段冲突返回 422；合法旧客户端可显式指定 Worker ID 切换 fixed，pool 的旧字段读值为 null

### Requirement: 冻结快照和资产来源
系统 SHALL 区分配置路由、派发目标和 Attempt 实际节点。Activation 及非订阅 Execution 冻结路由、环境、材料、资源、等待策略和凭据引用；T2、批次与自动 Retry 使用原快照。Pool 快照 MUST 保持原规则 revision，成员及瞬时容量不写入冻结集合。普通 Execution Replay 使用当前 Adapter 绑定创建新执行，依赖检查 Replay 保留原固定节点，响应显示绑定来源。

#### Scenario: 同部署 Clone 已配置路由
- **WHEN** 源 Adapter 已绑定固定 Worker 或 Pool
- **THEN** 副本保持停止，运行路由未绑定，用户须显式重绑；源绑定与其他既有复制规则保持

#### Scenario: 历史固定目标缺失
- **WHEN** 历史责任需要 Retry 或生成新 Outbox，却无法从冻结事实确定原 Worker
- **THEN** 保留原责任并阻止重新派发，当前 Adapter 或可变派发目标不能补作历史依据

#### Scenario: 停止态跨部署重新绑定
- **WHEN** 用户 Clone、使用模板或导入含有不可移植 Worker 或 Pool 引用的资产
- **THEN** 保持停止并提示显式重新绑定；同名 Pool 不自动匹配，取消保留原草稿

### Requirement: 首批仅允许停止态 Pool 规划
服务端 SHALL 允许授权用户管理 Pool 并保存停止态绑定。生产首次接受 Pool 业务前 MUST 在新责任事务中核对实际路由并拒绝交接；现有请求按原授权与幂等合同优先读回。普通执行、触发、Retry、Replay、单条 T2 与批次均适用。

#### Scenario: 停止态 Pool 草稿与运行请求
- **WHEN** 已保存 Pool 草稿的 Adapter 收到首次启用或执行请求
- **THEN** 返回明确拒绝原因，保留草稿，未新增 Activation、Execution、Admission 或 Outbox，原 Delivery、等待责任及重试预算保持

#### Scenario: 旧请求在当前配置变更后重送
- **WHEN** 已受理请求按原合同允许幂等读回，而当前 Adapter 已保存 Pool 草稿
- **THEN** 返回原责任；当前草稿不改变原绑定、目标或请求结果

### Requirement: 合法 W1 升级保留固定路由事实
部署 SHALL 在停止写入后由离线 owner 使用闭合的历史回执链，从支持的 W1 来源按 0090→0091 维护流程升级，并在独立观察提交状态后恢复运行角色。历史快照 MUST 仅按原执行或激活已记录的固定 Worker 回填。

#### Scenario: 当前 Adapter 的绑定与历史执行不同
- **WHEN** 升级时 Adapter 已改绑，或历史 Execution 缺少原固定目标
- **THEN** 有事实的快照标记 legacy 并保留原目标；缺失目标保持未分类，未证实的环境保持 unknown
