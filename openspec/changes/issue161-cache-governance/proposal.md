## Why

Worker 的版本依赖环境会持续占用磁盘，现有容量预留只限制安装，不能证明一个环境现在和删除期间都无人使用。Issue #139 要求在真实 RabbitMQ v3 链路建立使用、排队和恢复引用的删除协议，同时保留旧 ready 缓存命中，再提供容量策略与管理员入口。

## What Changes

- 检查点 A：为 VerifiedVersionCache 增加独立生命周期元数据、跨进程使用锁、Control 持久删除门禁与幂等删除记录；覆盖安装、加载、运行、queued/retry_wait、Incident、恢复及未确认清理责任。
- 所有创建或恢复引用与删除门禁串行化；已有 Adapter 清理、过期环境清理和损坏环境重建不得绕过新的本地安全原语。
- 保留旧 manifest 精确校验、只读内容、容量预留和原子发布。缺少新元数据仅表示未知；有界初始化不改缓存内容、不强制重装。
- 检查点 B：配置周期、最近使用保留、TTL、容量高低水位、磁盘余量和每轮预算。周期和容量自动回收默认关闭；未确认可重建、固定、离线或事实不明的环境保留。
- 分别报告版本环境、共享下载缓存、失败 staging。当前共享下载树仅报告并明确不支持自动删除；有确切归属和失活证据的 staging 使用独立规则。
- 复用 Worker 主动连接 Control 的鉴权与轮询边界，增加有界缓存快照、预览/清理/保护操作和结果上报；管理员在 Worker 页面查看占用、原因、操作状态与审计。

## Capabilities

### New Capabilities

- `worker-cache-safety`：缓存身份与生命周期分离、旧缓存兼容、使用锁、引用门禁、删除与恢复安全。
- `worker-cache-governance`：保守分类策略、容量与周期治理、Worker 通信、管理员操作和审计。

### Modified Capabilities

无。现有托管输入与 Workspace 规格继续生效，本变更不将其复制为新的权威合同。

## Impact

- 影响 Worker `cache.py`、`venv.py`、五语言依赖准备、`executor.py`、`agent.py`、`client.py`；Control 新引用/恢复入口、Worker API、模型与 Alembic；Web Worker 管理页、API 类型及双语文案；增加对应运行文档和测试。
- 新增持久 guard、缓存操作/审计和最近快照存储，采用增量迁移。Worker 生命周期文件位于缓存内容树外。旧 Worker 不获得新删除命令；自动策略默认关闭，混合版本仍可执行原有任务。
- 回滚前停用治理、排空或安全撤销未完成删除门禁，再回滚 Worker/Control；未完成门禁时拒绝删除相关新表。保留缓存卷及原 manifest，禁止清空缓存作为升级或回滚步骤。
- 非目标：#129 Worker Pool/跨服务器/HA、第一/二/四组功能、D13/watcher、依赖解析器重写、通用恢复框架；不删除 Adapter 代码、版本、配置、Execution 历史或托管输入文件。
- 交付为第三组一个集成分支内 A→B 串行提交；A 门禁通过后直接继续 B。组内设计和 Worker 子任务采用 `LOCAL_FAST`；第三组整体为 `REMOTE_RELEASE`，由集成负责人执行已授权 PR、部署与合并；#161 最终 head 与用户验收门禁保持独立，最终验收前不关闭 Issue。
