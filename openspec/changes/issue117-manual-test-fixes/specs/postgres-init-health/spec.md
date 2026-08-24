## Purpose

防止 PostgreSQL 因平台日志目录问题形成半初始化状态并被 Compose 健康检查误判为可用，确保 Control 只在目标 `dlr` 数据库真实可查询时进入依赖服务启动路径。

## ADDED Requirements

### Requirement: 首次初始化前校验 PostgreSQL 日志目录

PostgreSQL 首次 `initdb` 前 MUST 以容器内 postgres 用户的有效权限验证 `$DLR_PLATFORM_LOG_ROOT/postgres` 存在且可写。校验失败时启动 MUST 以明确错误退出，且不得继续执行首次初始化或报告可供 Control 依赖的 healthy 状态。

#### Scenario: 日志目录不存在
- **WHEN** PostgreSQL 首次启动且平台日志 postgres 子目录不存在
- **THEN** 服务在 initdb 前明确失败，用户可从日志识别目录问题，且不会进入误导性的部分初始化后 healthy 状态

#### Scenario: 日志目录不可写
- **WHEN** PostgreSQL 首次启动且 postgres 用户无法写入平台日志 postgres 子目录
- **THEN** 服务在 initdb 前明确失败，不能以目录修复后跳过初始化的半初始化状态继续运行

### Requirement: Healthcheck 必须验证目标数据库可查询

PostgreSQL healthcheck MUST 使用 `dlr` 用户连接目标 `dlr` 数据库并执行等价于 `SELECT 1` 的实际查询；仅成功响应 `pg_isready` 不得被视为 healthy。目标数据库不存在、连接失败或查询失败时 healthcheck MUST 为 unhealthy。

#### Scenario: 目标数据库不存在
- **WHEN** PostgreSQL 只完成部分初始化并存在 `dlr` 用户但不存在 `dlr` 数据库
- **THEN** healthcheck 返回失败，服务不被标记为 healthy

#### Scenario: 目标数据库可查询
- **WHEN** `dlr` 用户可以连接 `dlr` 数据库并成功执行查询
- **THEN** healthcheck 返回成功，且不改变数据库数据或业务状态

### Requirement: Control 不得被虚假 healthy 放行

Compose 的 Control 依赖条件 MUST 继续使用 PostgreSQL 的真实 healthy 结果；在目标库不可查询时 Control 不得因 `service_healthy` 被启动为正常可用路径。该修复 MUST 不新增数据库表、迁移或改变应用 API 合同。

#### Scenario: 半初始化阻断 Control
- **WHEN** PostgreSQL 进程可响应基础探活但目标 `dlr` 数据库不可查询
- **THEN** PostgreSQL 保持 unhealthy，Control 不因健康依赖被放行，部署失败原因可定位到目标库检查
