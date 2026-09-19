## MODIFIED Requirements

### Requirement: Managed Files feature flag 按完整能力门禁开放
当前已统一执行机制的产品部署中，`DLR_MANAGED_FILES_ENABLED` 未设置时 SHALL 默认开启，Settings、Compose 与示例配置 MUST 一致；显式 true/false SHALL 保留优先级。默认开启不豁免 ArtifactStore 持久化、文件输入、Lease/回收、容量和真实运行门禁，不改变旧部署已有显式 false。

#### Scenario: 未设置开关的新部署
- **WHEN** 新部署未配置该变量且满足现有运行与存储条件
- **THEN** Task 可选择托管文件、上传、保存并运行，不需要额外打开开关

#### Scenario: 显式关闭或开启
- **WHEN** 部署显式设置 false 或 true
- **THEN** 系统遵从指定值；false 保留既有允许/拒绝边界，none/json 不受影响，已有历史/文件和治理责任不被删除

#### Scenario: 旧部署升级
- **WHEN** 旧部署保留显式 false 并更新产品
- **THEN** 该部署保持关闭，文档说明如何显式改变配置并重新创建相关容器，不自动改写环境

#### Scenario: 开放能力验收
- **WHEN** 对修复版本判定默认开放完成
- **THEN** 验收包含合成 Excel/CSV/LOG/JSON、五语言文件读取、相关模板、持久化、配额及 Lease/回收，不以 capability ready 代替业务结果

#### Scenario: 仅后端上传完成
- **WHEN** 某候选只有 ArtifactStore/上传验证完成，Worker 文件执行与 cleanup recovery 尚未通过
- **THEN** 不能把局部实现作为可交付的默认开放版本；该未达门禁环境显式关闭文件能力，保留 none/json

#### Scenario: 开放前检查
- **WHEN** 对当前统一执行机制的修复版本完成开放门禁检查
- **THEN** 证据证明当前受支持 Worker capability、Lease/下载/清理/故障与五语言文件运行通过，不以改变默认值追认历史未通过的 Gate
