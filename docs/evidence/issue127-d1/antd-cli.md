# Issue #127 D1 Ant Design CLI receipt

Queried on 2026-08-28 before D1 component implementation. Every command exited
with status 0.

```text
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 info Card --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 demo Card --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 info Form --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 demo Form --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 info Upload --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 demo Upload --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 info Progress --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 demo Progress --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 info Tooltip --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 demo Tooltip --format json
```

Implementation-relevant 5.29.3 facts include controlled Upload `fileList`,
manual upload through `beforeUpload`, `Upload.LIST_IGNORE` for rejected files,
Progress `percent`/`status`/`size`, Form validation state, Card semantic styles,
and Tooltip focus trigger/disabled-title behavior.

After the queries, `web/package.json` still declared `antd` `5.29.3` and
`@ant-design/pro-components` `2.8.10`; neither manifest nor lockfile changed.
