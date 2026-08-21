# M5.10 Wave D Ant Design evidence

The Workbench and AI Assistant display changes use the checked-in Web baseline:

- React 19 + Vite
- `antd@5.29.3`
- `@ant-design/pro-components@2.8.10`
- `@assistant-ui/react@0.15.15`
- `@monaco-editor/react@4.7.0`

The pinned Ant Design CLI was queried before implementation:

```sh
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 info Button --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 info Tooltip --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 info Tabs --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 info Modal --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 info Drawer --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 info Descriptions --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 semantic Button --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 token Button --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 demo Drawer basic-right --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 changelog 5.29.3 --format json
```

The implementation uses the queried stable APIs: Button `icon`, Tooltip hover/focus
triggers, responsive Drawer/Modal string widths, responsive Descriptions columns,
Tabs navigation overflow, and explicit Monaco ARIA labels. No Ant Design 6, Ant
Design X, Umi, Tailwind, or replacement UI framework was introduced.
