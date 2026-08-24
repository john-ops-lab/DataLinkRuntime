## Purpose

区分平台管理员的全局 Credential 管理入口与普通 Adapter owner 的绑定能力，避免凭据绑定区域产生越权或误导性引导，同时保持既有后端权限合同不变。

## ADDED Requirements

### Requirement: 凭据新增提示必须按平台角色展示

凭据绑定区域 MUST 根据当前经认证的**平台角色**决定新增凭据提示，不得根据 Adapter owner 身份推断全局 Credential 管理权限。平台管理员 SHALL 保持现有提示及“打开系统设置”入口；非平台管理员 SHALL 仅显示以下文案，并不得显示“打开系统设置”链接：`如需新增凭据，请联系管理员前往「系统设置 → 凭据管理」新建。`

#### Scenario: 平台管理员看到既有引导
- **WHEN** 平台管理员打开 Adapter 的凭据绑定区域
- **THEN** 页面继续显示原有新增凭据提示和打开系统设置入口

#### Scenario: 非平台管理员看到联系管理员引导
- **WHEN** 非平台管理员打开 Adapter 的凭据绑定区域
- **THEN** 页面显示指定联系管理员文案且不渲染打开系统设置链接

### Requirement: 凭据绑定权限保持原有边界

本 change MUST NOT 放宽非平台管理员对已有 Credential 的绑定、读取绑定元数据或编辑绑定的既有权限，也 MUST NOT 暴露全局 Credential CRUD；隐藏入口不得被视为后端授权替代。

#### Scenario: Adapter owner 绑定已有凭据
- **WHEN** Adapter owner 对已有且可见的 Credential 执行当前允许的绑定操作
- **THEN** 操作继续按既有 edit/read 与共享权限判定，不因新增提示变化而获得全局 Credential 创建或管理权限

#### Scenario: 非授权用户尝试全局凭据管理
- **WHEN** 非平台管理员直接请求全局 Credential 管理能力
- **THEN** 后端继续按现有 admin-only 合同拒绝请求，且前端提示不泄露可用的管理入口
