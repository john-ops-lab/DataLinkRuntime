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
