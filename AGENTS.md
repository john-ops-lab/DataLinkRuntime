<!-- antd-cli setup start -->
## Ant Design CLI Skill

Use the shared Ant Design skill at `.agents/skills/antd/SKILL.md` before working on Ant Design code in this repository.

The skill teaches agents when and how to call `@ant-design/cli` commands such as `antd info`, `antd doc`, `antd demo`, `antd token`, `antd semantic`, and `antd changelog`.

## DLR Ant Design version boundary

- This repository's Web baseline is `react` 19, `antd` **5.29.3**, and `@ant-design/pro-components` **2.8.10**. Keep these exact versions in the Web manifest unless a later Wave explicitly changes the contract.
- Use the project-local skill and query the exact snapshot before writing or changing Ant Design code. The reproducible CLI form is `npx --yes @ant-design/cli@6.6.1 --version 5.29.3 <command> --format json`.
- For API, Demo, Design Token, Semantic DOM, and changelog questions, run `info`, `demo`, `token`, `semantic`, and `changelog` respectively. Do not infer an API from memory or from the unversioned latest docs.
- Ant Design 6, Ant Design X, Umi, Tailwind, and a second general-purpose UI framework are outside the current M5.10 Wave A contract.

<!-- antd-cli setup end -->

## 本机 PR 验收交付

- 本机验收入口及部署参数仅从私有配置读取，禁止写入公开 PR 或仓库。控制器源码与操作契约见 [docs/zh-CN/local-preview.md](docs/zh-CN/local-preview.md) 和 `tools/local-preview/`。
- 用户要求提交 PR 并提供本地验收时，在 PR 建好后执行 `python3 "$DLR_PREVIEW_HOME/preview.py" select <PR>`，由控制器等待当前 HEAD 的 CI 通过再更新；查询 `status`，报告已部署 SHA 与真实验证结果。不要另建普通后继 PR 的重复环境，不自动合并。
- 保留固定 Compose project、持久卷、Token 与 Master Key。历史分叉、迁移不兼容或 attention 时先诊断，不绕过检查，不自动回退数据库。
