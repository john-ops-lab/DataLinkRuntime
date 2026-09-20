## Purpose

定义第二组产品修复在保留第一组运行责任及审计的固定环境中进行一次性同 schema 更新的行为边界，并要求精确版本审查、双入口验证、旧资产完整保全和真实探针后才声明部署成功。

## ADDED Requirements

### Requirement: 第二组更新绑定独立批准的精确候选
系统 SHALL 仅对本组受限 `audited-group2-same-schema-v1` / manifest v4 支持同 `0040_issue152_dispositions` 更新，MUST 将具体批准、独立代码审查、完整 Git 差异、迁移图、controller、镜像与精确候选 CI 绑定为一次性准入依据；普通模式、v2 与第一组 v3 规则 MUST 保持。

#### Scenario: 精确对象发生变化
- **WHEN** 安装、规划、选择或最终切换时发现 base/head/tree、任一路径 status/mode/blob、迁移图、controller、镜像或 CI 与独立冻结记录不符
- **THEN** 系统拒绝推进，不从当前 diff 自行生成批准，不自动重绑 manifest 或扩展到后续组

#### Scenario: 成功后再次选择其他候选
- **WHEN** 已消费的 manifest 被用于其他 head 或未完成事务被要求标记成功
- **THEN** 系统拒绝，不复用第一组或本组旧探针与成功证据

### Requirement: 更新全过程保留原责任及全部旧资产
系统 MUST 在一致的只读快照中保护全部原责任、完整审计、业务及账号会话行，并保护旧文件、权限、材料、卷、凭据和显式配置；fresh baseline SHALL 先与独立封存事实核对。备份、停机后真实 idle kernel、同 schema no-op 及启动前后保全 MUST 完整通过，未知漂移 MUST 阻断成功，禁止通过删除责任、降级数据库或重置状态绕过。

#### Scenario: 旧责任或审计改变
- **WHEN** 原 queued、cleanup、terminal、零 Attempt pending 占位、完整审计或旧用户/会话/资产发生未获准变化
- **THEN** 保留失败证据及 attention，不能写 ready/current 或消费 manifest

#### Scenario: 新探针自然完成并收尾
- **WHEN** 本候选完成全新 RabbitMQ→Worker 实际执行与自然 cleanup
- **THEN** 系统依据当次精确对象、窗口与内容验证新增量，全部旧行及旧文件内容仍受保护；未知文件或全局时间戳豁免不能用于通过后置检查

### Requirement: 双入口沿原绑定部署并验证实际身份边界
第二组模式 SHALL 沿既有显式私有配置和实际容器绑定启动账号入口，使用与 Token 入口相同的冻结 Web image；MUST 保留原 loopback、command、network、mount 和旧日志前缀，不能新增默认端口或新配置。部署成功判据 MUST 包含真实代理 rewrite、认证、CSRF 及内部前缀隔离，不以首页或内部探针替代；真实普通账号 ACL、上传和页面业务 SHALL 在部署后作为单独的合并前门禁，不在正式部署探针窗口创建账号。

#### Scenario: 原账号容器停止且镜像较旧
- **WHEN** 原绑定与显式配置一致且符合已批准范围
- **THEN** 账号容器与其他应用一起更新到候选 Web image；实际 image ID、绑定、健康和代理身份边界通过后才可声明成功

#### Scenario: 账号入口失效或跨入口授权
- **WHEN** 账号入口不健康、绑定或镜像漂移、Token/账号身份互通，或缺失/错误 CSRF 写请求未被拒绝
- **THEN** 部署或健康检查失败，不能仅因 Token Web 可用而报告整体正常

#### Scenario: 已成功版本恢复
- **WHEN** current SHA、ready 事务、有效 v4 receipt 与已消费 manifest 一致并请求同 SHA recover
- **THEN** 恢复并重新核验双入口，不再生成业务探针；无 v4 receipt 的旧模式保持原恢复行为

### Requirement: 账号业务验收增量独立归属且不修改旧账号
业务验收 MUST 只操作本轮新对象并保留旧用户、权限、密码及会话；没有适用授权会话时 SHALL 仅使用已批准的唯一普通测试用户备选。首次改密、会话及停用仅作用于该新用户，收尾 MUST 正常清理自有对象、登出并停用，保留用户行与真实残留记录。

#### Scenario: 普通用户验收与 ACL 拒绝
- **WHEN** 使用本轮新普通用户完成上传、保存、运行及权限负例
- **THEN** 正例属于其自有新对象，负例使用另一新对象，不使用原保留责任作为写入目标；这些对象与正式部署探针分开留证
