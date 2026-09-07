## Why
预览控制器在目标 PR 关闭后需要显式切换后继 PR，避免重复环境造成入口与数据分裂。需要把已存在的本机 CD 纳入版本管理并支持显式切换目标。

## What Changes
- 固定私有配置指定的入口、Compose project、数据卷与凭据；通过 CLI 登记当前待验收 PR。
- 当前完整 HEAD 的最新 CI 成功后构建 SHA 镜像；切换前重新检查目标、CI、Git/Alembic 向前兼容性。
- 等待执行空闲，停写后备份、迁移、验证；故障持久记录，迁移后禁止盲目数据库回退。
- 记录 desired/candidate/deployed、备份及恢复信息；相同 SHA 的 CI 重跑不重复部署。
- 迁入仓库脚本、测试、操作文档及 AGENTS 入口；安装时备份并保留原本机配置。

## Capabilities
### New Capabilities
- `local-preview-delivery`: CI 门控的固定入口本机预览交付与恢复。
### Modified Capabilities
无。

## Impact
影响维护工具与本机部署流程，不修改业务 API、数据库模型或 Web 框架。在现有数据环境中升级目标 PR；保全重复环境的用户资产后再退役，保留必要恢复材料。不自动合并 PR。
