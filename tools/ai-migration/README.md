# Issue #213 B0：Pydantic AI 兼容实验

此目录是可独立安装、锁版本的实验项目，使用合成材料、真实 SDK 和仅监听 loopback 的协议模拟器。生产后端的依赖、运行路径和部署保持原状。

**当前结论：B0 代表性门槛通过，依序进入 B1。** 110 个用例通过；新增正确行为断言证明公开工具桥接及独立工作所有者能补齐原缺口。原 3 个 `gap` 用例保留首批薄包装的失败观察，不单独计为门槛通过。详见 [B0 报告](../../docs/testing/issue213-pydantic-b0.md)及[版本化规划](../../openspec/changes/migrate-ai-assistant-to-pydantic-ai/proposal.md)。

## Reproduce

在仓库根目录运行，需要 Python 3.13 和 uv：

```sh
uv sync --project tools/ai-migration --frozen --python 3.13
uv run --project tools/ai-migration --frozen ruff check tools/ai-migration
uv run --project tools/ai-migration --frozen ruff format --check tools/ai-migration
uv run --project tools/ai-migration --frozen mypy --config-file tools/ai-migration/pyproject.toml tools/ai-migration/probe_support.py tools/ai-migration/guards.py tools/ai-migration/control_probe.py tools/ai-migration/snapshot.py tools/ai-migration/dispatch_model.py tools/ai-migration/owned_work.py tools/ai-migration/final_probe.py
B0_TEST_DATABASE_URL=<DEDICATED_POSTGRESQL_URL> uv run --project tools/ai-migration --frozen pytest tools/ai-migration/tests -q
```

没有真实模型调用、账号或模型费用。P6 的一个用例需要专用 PostgreSQL，通过 `B0_TEST_DATABASE_URL` 注入；未提供时该项跳过，不能声称 P6 全部通过。它只创建并删除随机测试表，不访问业务表；CI 使用独立 PostgreSQL service。其余配置使用保留域名，实际 SDK 请求只发往本地模拟器；DNS 失败用例替换解析器并在退出时释放同步工作。所有线程、server、client 和借用的传输许可由测试清理。不要针对真实部署运行这些夹具。

需要只查看已知缺口时：

```sh
uv run --project tools/ai-migration --frozen pytest tools/ai-migration/tests -m gap -q
```

这些用例断言的是框架和薄桥接的当前行为。例如未知工具绕过参数 hook，或取消后的 DNS 继续存活。将来完成适配后，应增加正确行为的兼容断言，再更新此观察；不能仅删除用例获得绿灯。

## Contents

| 文件 | 职责 |
|---|---|
| `pyproject.toml` / `uv.lock` | 精确锁定实验依赖；继承后端基线约束，仅隔离项目将冲突的 websockets 改为 16.0 |
| `probe_support.py` | 三协议模拟器、真实 SDK 显式装配、code-only 输出模型和合成媒体 |
| `guards.py` | 公开 transport / capability 扩展点实验；包含已知 DNS 许可过早释放缺口，禁止直接作为生产接入 |
| `control_probe.py` | 复用真实 DLR dispatcher / 预算状态的代表桥接；通用迭代仍由 Agent 驱动 |
| `dispatch_model.py` | 公开 WrapperModel 在框架边界映射内部调度；SDK 保留原白名单 Schema、工具名、原参数与关联 ID |
| `owned_work.py` | 有界独立工作所有者和公开同步 HTTP transport；取消后不提前归还许可，实际结束再关闭 |
| `final_probe.py` | 一次无工具格式纠正、有限收尾及安全失败；非法原文不进入纠正上下文 |
| 新兼容、资源、领域进度及纠正测试 | 原缺口正确行为断言、Reader pin、PostgreSQL Session/撤权、知识/补读/收尾与实际请求数 |
| `tests/test_protocols.py` | 协议、鉴权、输出模式、媒体、严格解析、显式关闭重试 |
| `tests/test_control.py` | 串行、batch 预检查、坏参数记账、重复、失败停止和未知工具缺口 |
| `tests/test_lifecycle.py` | 跨 loop 共享容量、现有 Catalog 路径、取消和合成请求隔离 |
| `snapshot.py` / `baseline.json` | 固定源码/Prompt/材料 hash、依赖和许可证元数据、历史失败锚点 |

重新生成本机基线元数据：

```sh
uv run --project tools/ai-migration --frozen python tools/ai-migration/snapshot.py --output tools/ai-migration/baseline.json
```

该文件记录本机平台和依赖安装大小；其他平台重新生成的大小和条件依赖可能不同。它不读取部署配置、凭据、用户附件或私有验收材料，也不是功能验收 PASS 凭据。

## Dependency image measurement

[image-cost.json](image-cost.json) 保存 Linux arm64 测量镜像的 Docker `Size`、镜像 ID、锁/requirements/Dockerfile hash 与 OpenAPI 导入快照。相同 Python 基础镜像、相同源码、仅运行依赖的增量为 17,948,678 bytes（约 17.1 MiB）。这是依赖测量镜像，不是最终发布镜像或业务验收。

[image-cost/](image-cost/) 保留实际 Dockerfile 和两份精确 requirements。复现时将三份文件复制到临时目录 `tools/ai-migration/.image-cost/`，把原基线后端锁及实验锁中适配 Linux arm64 的 wheel 下载到其中 `wheels/`，逐个核对锁内 SHA-256，再用相同 Dockerfile 构建默认 baseline 和 `DLR_REQUIREMENTS=tools/ai-migration/.image-cost/candidate.txt`。wheel 通过只读 BuildKit mount 使用，不进入镜像。候选新增固定 Pydantic AI/SDK 依赖并将 websockets 调整为 16.0；发布镜像成本在正式接入后再核对。
