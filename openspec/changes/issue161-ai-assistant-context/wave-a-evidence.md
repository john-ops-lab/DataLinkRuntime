# Wave A 本地验证记录（2026-10-01）

- 五语言 × 工具/知识三态 × 附件/原生图片四态，共 60 组 Provider 消息与 `40ba95b` 的 `_assist_messages()` 结果逐字节一致。
- 隔离 PostgreSQL 中运行现有 AI 相关测试与新 Builder 测试：389 passed。最终资源换行修正后，单独重跑新测试：21 passed。
- Backend 全量 Ruff check、Ruff format check、Mypy 均通过。
- `uv build` 产出 wheel 与 sdist；逐一检查均包含 `system.md`、`adapter.md`、`tools.md`。将 wheel 安装至独立虚拟环境并切换 `/tmp` 工作目录后可读取资源。
- 使用 `docker/control.Dockerfile` 隔离构建镜像 `sha256:f0bba9ae736810b862e07e8bf603c8037c8ff3e60eb378cc65ee06a832b6871e`；在 `/tmp` 工作目录运行容器可读取三个资源。wheel 与容器得到相同 revision：`186fd5a8f1a9de1002b1327a7cbf2d14a956b441ca58cbca064c4a4f61f29698`。
- 缺失、空白、坏 UTF-8 资源测试通过；坏资源在模块导入时失败。诊断仅含 revision、资源字节数和能力开关。
