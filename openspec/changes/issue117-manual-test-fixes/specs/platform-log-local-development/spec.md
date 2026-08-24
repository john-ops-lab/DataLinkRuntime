## Purpose

为本地开发者提供可写且不依赖 Linux 系统目录的 DLR 平台日志配置说明，并让 `.env.example`、快速开始和平台日志部署文档对五个日志子目录及 postgres 权限要求保持一致。

## ADDED Requirements

### Requirement: 本地示例必须指向用户可写日志根目录

`.env.example` MUST 提供或在注释中明确一个当前用户可写的本地 `DLR_PLATFORM_LOG_ROOT` 示例（例如 `./platform-logs`），并明确与 Linux 生产部署使用的绝对路径示例不同。示例 MUST NOT 建议 `chmod 777` 或同等过度放权方案。

#### Scenario: 本地开发者准备日志根目录
- **WHEN** 用户按照 `.env.example` 开始本地 Compose 部署
- **THEN** 用户可以选择仓库内或其他当前用户可写的日志根目录，不会被默认的 Linux 系统路径隐式阻塞

### Requirement: 快速开始说明五个日志子目录和权限

README 快速开始 MUST 在启动 Compose 前说明平台日志 bind mount/目录准备，并明确以下五个子目录：`control/`、`worker/`、`web/`、`account-web/`、`postgres/`。文档 MUST 明确 `postgres/` 必须对容器内 postgres 用户可写，并区分本地开发与 Linux 生产部署的权限处理方式。

#### Scenario: 按快速开始准备本地目录
- **WHEN** 用户阅读 README 并执行首次本地启动
- **THEN** 用户能按文档创建五个子目录、准备可写权限并在启动前完成平台日志 bind mount

### Requirement: 平台日志文档与示例保持一致

README、`.env.example` 与 `docs/deployment/platform-logs.md` 对 `DLR_PLATFORM_LOG_ROOT`、五个子目录、postgres 写权限和生产部署区分的描述 MUST 一致；文档调整不得泄露凭据或改变日志脱敏/轮转合同。

#### Scenario: 文档交叉核对
- **WHEN** 用户对照三个配置/文档入口准备本地或生产部署
- **THEN** 不会看到互相矛盾的根目录、子目录或权限要求，并能识别不可写目录会导致启动失败
