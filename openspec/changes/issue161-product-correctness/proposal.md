## Why

#161 第二组修复现有功能中的数据正确性和可操作性问题：JSON 数字/布尔类型保存失真、合法 null 输出被隐藏、无法取消选中 queued 记录、跨页面计划锁定不同步，以及托管上传代理边界、默认开关、依赖源地址和 Java 文档合同不一致。第一组已提供取消与可靠运行合同，本组沿用这些合同并分别验证所有承接子项。

## What Changes

- #153（含原 #156）：修复 JSON 输入及 Schedule 兼容镜像的类型持久化，保留 revision、JSON null/SQL NULL 和不可变历史；实时与历史输出区分完整值、合法 null、明确无输出、历史信息不足和截断。
- #155（含原 #158）：历史详情取消固定发起时的 Execution ID；统一刷新后端运行锁和计划状态，保留所有编辑草稿、权限保护、409 及合法解锁动作。
- #150（含原 #154）：先修复 Token/账号两套托管上传路由，以固定有界总请求上限覆盖应用最大净文件和 multipart 预算；随后统一默认开启，保留显式 false 及 ArtifactStore、配额、Lease、GC 和回滚合同。补齐实际 Excel 模板验收发现的 Python 模块加载兼容问题，使延迟注解和 dataclass 按标准导入语义工作。
- #157：按四类依赖源的现有地址语义校验创建及 PATCH 合并后的最终组合；前端字段可见、错误脱敏、旧非法记录可修复/删除，保存不探测网络。
- #159：以实际 Java Context 公共字段和 logger 方法纠正文档与示例，并对真实 Runtime 编译运行。
- 按以上顺序保留独立提交与测试检查点，同组只有一个最终 PR；逐项机器检查不替代最终 head 的 CI、独立 Review、运行验证和用户手工验收。
- 根据本组专项批准，新增仅供本组精确候选的一次性 `audited-group2-same-schema-v1` / manifest v4 保全部署合同；保留第一组责任、完整审计和全部旧资产，沿原绑定启动账号入口，并完成动态探针后的保全验证。
- 为本组首次启动后、正式探针前失败的事务准备一次受限软件恢复入口：保留数据和失败原件，恢复最后成功的 PostgreSQL、Control、Worker、Web 软件，停止并保留当前 account-web；完成控制面对账后再重新规划正常 v4 更新。代码准备属于本组收尾，实际事故工具传输、软件恢复及控制面提交仍须针对精确请求单独批准。
- 对本组已恢复旧软件、但因应用健康等待失败而丢失部分内存阶段证据的已绑定事故，按用户接受的证据例外准备一次受限事后收尾。明确区分保存原件、执行路径推导、不可恢复的历史缺口与当前完整保全事实；不再次停止或启动服务、不修改业务数据，不冒称原完整恢复链成功。最终工具、独立审查、CI、缺口条款及具体动作仍绑定新的专项执行批准。

## Capabilities

### New Capabilities

- `execution-output-presence`: 现有输出元数据的完整值、空值、缺失与历史歧义显示合同。
- `workbench-runtime-consistency`: 指定执行取消及不丢失草稿的跨页面运行锁同步。
- `managed-input-proxy-boundary`: 两套入口有界上传路由与后端净文件策略一致性。
- `package-source-address-validation`: 四类源创建/修改的最终有效地址校验与冷环境选源验证。
- `java-context-documentation-contract`: AI 文档、关联示例与真实 Java Runtime 的编译执行合同。
- `incident-preserving-upgrade`: 增加第二组专用同 schema 保全更新及双入口验收要求；不改变第一组已有合同。

### Modified Capabilities

- `adapter-input-config`: 补充数字与布尔类型的真实 PostgreSQL 持久化及不可变执行快照要求。
- `input-compatibility-rollout`: 将托管文件的当前产品缺省改为开启，保留显式关闭和非破坏回滚；不重写历史 Wave/协议迁移条款。

## Impact

影响 Control 的 InputConfig 更新、依赖源服务/字段错误、Java AI 文档与 Settings 缺省值，Web 工作台、执行历史、输出和源管理，以及两套 Nginx、Compose 缺省值、Python harness 的模块登记、示例配置、双语说明和相应测试。复用现有架构，无新增库、通用状态管理、Runtime API 或管理页面开关；Ant Design/React 版本不变。

预计无需 Alembic 迁移，不批量修复历史 JSON/输出、旧非法源或部署环境变量；现有显式 false 不被默认值覆盖。若后续证据要求 schema/Worker wire protocol 或持久运行责任变更，须重新审查方案与部署门禁，不能混入本组最小实现。

第二组包含后端、Nginx 和 Compose 变化，超出第一组 `audited-web-same-schema` 保全更新合同；本组已获针对具体范围的专项批准，同 schema 本身仍不是部署授权。配套只修改 `tools/local-preview/` 下的 `preview.py`、`deploy.sh`、`carry_forward.py`、`tests/test_preview.py`、`tests/test_carry_forward.py` 及 `docs/zh-CN/local-preview.md`，同步本组 proposal/design/tasks 和新增 incident-preserving-upgrade delta。安装器、验证器、资产工具、端口与配置字段不扩展。最终实现仍须独立审查、冻结完整 Git 差异与精确 HEAD CI 后，由官方安装器和控制器执行。产品实施、最终 PR、合并、部署、用户验收和 Issue 关闭分别记账，主 Issue 保持开放，原 #156/#158/#154 归档状态保持不变。

非目标：#129、第三组缓存治理、第四组 Prompt/会话改造、外部 PyPI 准备故障、S3 Java 原模板故障、托管上传自动重写 Nginx、STAGED 离页提示/过期提示扩建，以及第一组运行状态机再设计。

事故入口不修改安装器、不替换正式安装的控制器、不恢复数据库备份、不执行迁移或探针，也不提供通用失败事务续跑或自动回退。它只能从精确审查与 CI 通过的干净仓库运行，将同提交工具暂存到私有事故目录；失败候选始终保持失败身份。正式部署与双入口验收仍须通过原 v4 流程。
