## Purpose

定义现有工作台与执行历史在并发和跨页面使用下的一致性：用户能够取消明确选择的排队记录，并从后端权威运行状态获得一致的编辑限制；后台刷新和迟到响应不能破坏草稿或作用到错误对象。

## ADDED Requirements

### Requirement: 历史取消固定目标并复用现有取消合同
有操作权限的用户 SHALL 能从指定 queued Execution 的历史详情取消该记录；操作 MUST 固定发起时的 ID 并复用服务端权限、取消错误码和状态机。

#### Scenario: 保持 active 并取消 queued
- **WHEN** 同一 Adapter 存在 active A 与 queued B，用户在 B 详情点击取消
- **THEN** 请求目标是 B，B 状态与资源按既有合同收敛，A 不受影响

#### Scenario: 请求期间切换详情
- **WHEN** 用户取消 B 后切换到 C 或关闭详情，B 请求稍后完成
- **THEN** 不取消 C、不把 B 的状态或错误覆盖到 C，列表可重新读取 B 的最终状态

#### Scenario: 重复或并发取消
- **WHEN** 重复点击、取消与 claim/terminal 并发、权限被撤销或记录已终态
- **THEN** 页面显示可理解的进行中/最终结果，服务端最终校验且不重复释放资源

### Requirement: 所有受保护编辑使用统一权威锁
页面 SHALL 同步后端 Schedule/Webhook 启用及 active Attempt 形成的运行锁，所有受保护操作共享该锁；MUST 保留服务端 409 和权限保护，纯 queued/retry_wait 不额外锁住当前配置。

#### Scenario: 真实双页面启用计划
- **WHEN** 同一 Task 在两个真实页面打开，另一页面启用计划，编辑页随后完成状态同步
- **THEN** 保存、代码/依赖/运行配置、Worker、超时和输入等受保护控件一致禁用，并提供可访问的原因

#### Scenario: 正确解除
- **WHEN** 计划停用且 active 锁解除
- **THEN** 受保护操作恢复可用，停用计划和停止执行等合法解锁动作在此前仍可用

#### Scenario: 保存与启用竞争
- **WHEN** 页面仍未同步时提交保存，而后端已锁定
- **THEN** 后端拒绝，页面重新读取权威状态并解释原因，未保存内容保留

### Requirement: 后台状态同步不覆盖草稿
后台刷新 SHALL 只更新权威状态与安全的已保存基线；有 dirty 的代码、依赖、运行参数、输入和 Schedule 表单 MUST 保留，旧 Adapter/旧请求响应不得替换新选择。

#### Scenario: dirty 页面获得新锁
- **WHEN** 用户有未保存编辑，后台轮询、窗口恢复焦点或另一页面操作触发状态同步
- **THEN** 草稿内容和 dirty 标记仍存在，锁及时更新，解锁后用户可继续编辑

#### Scenario: 临时读取失败与迟到响应
- **WHEN** 状态读取失败或旧请求在切换 Adapter 后返回
- **THEN** 失败不会误解锁，迟到结果不会覆盖新 Adapter，下一次有界读取可恢复
