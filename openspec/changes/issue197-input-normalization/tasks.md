# Tasks
## 1. 后端输入边界
- [x] 1.1 实现规范化、冲突定位和服务接入；纯函数与 PostgreSQL API/Schedule/import 测试验证类型、计数、幂等和事务。
- [x] 1.2 添加响应计数与合同说明；ruff、mypy 和相关回归验证。
## 2. Web 保存
- [x] 2.1 实现解析后清理、提示、错误定位和响应范围保护；工具及组件测试覆盖特殊键和迟到响应。
- [x] 2.2 中英文反馈与 API 类型；lint、typecheck、test、build 验证。
## 3. 集成验收
- [ ] 3.1 主 Agent 真实 Chrome 五语言保存、重载、执行及独立 oracle 验证，并记录精确 SHA。

## Verification evidence
- Backend：`uv sync --frozen`；`uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy` 通过（176 源码文件）。
- 任务专用 PostgreSQL 16、`DLR_TEST_DATABASE=dlr_issue197_test`：`uv run pytest tests/test_issue197_input_normalization.py tests/test_issue127_a0_input_config.py tests/test_issue127_a1_unified_input.py tests/test_issue127_d2_clone.py tests/test_portable_packages.py -q` 首轮 115 passed；新增 strict 大小字段校验后单独 `test_issue197_input_normalization.py` 22 passed。
- Web：`npm ci`；`npm run lint`、`npm run typecheck`、`npm run build` 通过，保留构建已有大 chunk 提示。
- Web 专项：Node 24 下 `npm run test -- --run src/json-input-normalization.test.ts src/components/TaskRunSettingsPanel.a2.test.tsx src/components/TaskRunSettingsPanel.d1.test.tsx src/components/TaskRunSettingsPanel.d2.test.tsx` 46 passed。覆盖中英文计数、服务端实际值、特殊键、冲突保留草稿、迟到回复保留新草稿及托管文件回归。
- Ant Design CLI 6.6.1 / snapshot 5.29.3：Alert info/demo 查询完成；组件 lint 为 0 issues。
- 扩大检查 `npm run test` 在本机 Node 23 下持续输出 `TimeoutNaNWarning` 并有 worker 长时间高 CPU，约三分钟后显式终止，exit 143；全套未完成，不宣称通过。随后必要专项在依赖支持的 Node 24 下通过。
- 尚未完成真实 Chrome、五语言真实运行或业务验收；3.1 保持未勾选，由主 Agent 在精确集成 SHA 上执行。
