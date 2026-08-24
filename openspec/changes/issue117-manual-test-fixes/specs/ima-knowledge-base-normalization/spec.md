## Purpose

在 Tencent ima adapter 边界兼容真实 `data.info_list` 的 `kb_id/kb_name` 字段与既有 `id/name` 字段，向知识库列表和连接测试提供稳定的非空规范化结果，并继续拒绝不完整响应。

## ADDED Requirements

### Requirement: ima 列表字段必须按新旧契约归一化

当 ima 返回成功响应且 `data.info_list[]` 中存在知识库项时，adapter MUST 将知识库 ID 按 `kb_id` 优先、`id` fallback 归一化，将知识库名称按 `kb_name` 优先、`name` fallback 归一化。归一化结果 SHALL 提供非空的统一 ID/名称供列表、连接测试和后续知识检索链路使用，并保留现有 `kb_id` 作为搜索所需的真实标识。

#### Scenario: 真实 ima 字段响应
- **WHEN** `info_list[]` 使用 `kb_id`、`kb_name` 及其他真实响应字段且 HTTP 200、`code=0`
- **THEN** 知识库列表成功返回对应的非空统一 ID/名称，连接测试可以继续刷新列表

#### Scenario: 既有 id/name 响应
- **WHEN** `info_list[]` 使用当前源码声明的 `id`、`name` 字段且缺少 `kb_id`、`kb_name`
- **THEN** adapter 通过 fallback 继续返回对应的非空统一 ID/名称，不破坏既有响应兼容性

### Requirement: 不完整 ima 响应继续严格失败

当某个知识库项缺少 ID 的两种候选字段之一，或缺少名称的两种候选字段之一时，adapter MUST 拒绝该响应并返回稳定的 `ks_response_invalid` 错误；不得把 `None`、空字符串或未经验证的对象继续交给前端或搜索链路。严格校验 MUST 不回显凭据、完整响应或敏感原始 payload。

#### Scenario: 两套字段均缺失
- **WHEN** `info_list[]` 中的项同时缺少 `kb_id/id` 或同时缺少 `kb_name/name`
- **THEN** 列表请求稳定失败并映射为 `ks_response_invalid`，不返回部分伪造列表

#### Scenario: 搜索继续使用规范化 ID
- **WHEN** 列表使用 `kb_id` 归一化成功后用户发起知识检索
- **THEN** 搜索请求使用该真实 `kb_id`，列表兼容修复不改变已有鉴权失败和搜索成功的错误/成功映射
