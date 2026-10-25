# Spec Delta

## ADDED Requirements

### Requirement: 事件输入与 Task 当前输入隔离
订阅接收 SHALL 使用独立事件 Envelope，MUST 不覆盖 AdapterInputConfig 的来源、JSON、文件绑定或 revision，也不得将事件入口开放为普通手动 per-run 输入覆盖。订阅停用排空后的手动运行 SHALL 继续读取当前已保存 InputConfig。

#### Scenario: 接收事件不改变手动测试输入
- **WHEN** 一个已保存 JSON 输入的 Task 接管事件
- **THEN** 事件 Execution 使用其 Envelope，原 InputConfig 内容与 revision 不变

#### Scenario: 恢复普通手动测试
- **WHEN** 订阅停用并排空后用户运行一次
- **THEN** 仍使用 Task 已保存输入及原有校验，不能自动复用上一条事件

### Requirement: 订阅运行锁覆盖当前输入写入和触发互斥
订阅启用、接收停止中或存在未完订阅责任时，系统 MUST 拒绝当前输入写入与普通手动运行；绑定订阅的 Adapter MUST 不切换为 Schedule/Webhook。中心运行锁的详情和列表 SHALL 返回一致结果，不改变未绑定 Adapter 的原有行为。

#### Scenario: 页面尚未刷新而后端已接收事件
- **WHEN** 用户绕过前端禁用状态修改输入或手动执行
- **THEN** 后端返回稳定运行锁/订阅排空错误，不创建混入的任务

#### Scenario: 上传未绑定文件
- **WHEN** 订阅启用期间用户上传未绑定的 STAGED 文件
- **THEN** 仍可按既有上传治理合同暂存，但不能绑定为当前输入或通过上传解除运行锁
