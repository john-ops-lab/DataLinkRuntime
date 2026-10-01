# Wave A 本地验证记录（2026-10-01）

- 五语言 × 工具/知识三态 × 附件/原生图片四态，共 60 组 Provider 消息与 `40ba95b` 的 `_assist_messages()` 结果逐字节一致。
- 隔离 PostgreSQL 中运行现有 AI 相关测试与新 Builder 测试：392 passed。新测试检查 Markdown 无动态占位符、深层不可变消息、独立 Provider 副本和 typed 非敏感诊断。
- Backend 全量 Ruff check、Ruff format check、Mypy 均通过。
- `uv build` 产出 wheel 与 sdist；逐一检查均包含 `system.md`、`adapter.md`、`tools.md`。将 wheel 安装至独立虚拟环境并切换 `/tmp` 工作目录后可读取资源。
- 使用 `docker/control.Dockerfile` 隔离构建镜像 `sha256:0d2080fc20ccf66ff5b297c00df81655e2817c79a8c79ef33188d3a5ba46794d`；在 `/tmp` 工作目录运行容器可读取三个资源。wheel 与容器得到相同 revision：`5903b1ff9b96c83a10503604c4153ceaf6491906ba551d2bf376da52f2c9d08f`。
- 缺失、空白、坏 UTF-8 资源测试通过；坏资源在模块导入时失败。诊断仅含 revision、资源字节数和能力开关。
