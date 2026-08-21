# M5.10 Wave C — Ant Design evidence

## Pinned versions

- React: 19
- Vite: checked-in Web manifest
- `antd`: `5.29.3`
- `@ant-design/pro-components`: `2.8.10`
- Ant Design CLI: `@ant-design/cli@6.6.1`, queried with the repository-required version flag

## Reproducible CLI queries

All Ant Design component queries used this form, with `COMPONENT` replaced by
the component name:

```sh
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 info COMPONENT --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 demo COMPONENT --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 token COMPONENT --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 semantic COMPONENT --format json
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 changelog --format json
```

The exact snapshot was queried for `Table`, `Form`, `Input`, `Select`,
`Modal`, `Drawer`, `Empty`, `List`, `Pagination`, and `Space`. The reproducible
outputs were kept in `/tmp/dlr-m510-antd-queries` during implementation; this
report records the source/version and the decisions that reached the checked-in
implementation. The 5.29.3 changelog result contained a Breadcrumb fix and no
Wave C component API change.

The Ant Design CLI does not index ProComponents (`ProTable`, `QueryFilter`,
`ProForm`, `ModalForm`, or `DrawerForm`); those APIs were checked against the
installed `@ant-design/pro-components@2.8.10` package declarations and source.
Its peer range includes React 19-compatible React versions and antd 5, so no
dependency upgrade or second UI system was introduced.

## Applied component choices

- `QueryFilter`: Catalog, Credential, Package Source, and User Management
  filtering, with responsive vertical layouts and distinct accessible filter
  names from edit fields.
- `ProTable`: User Management, Credential, Package Source, and Knowledge Base
  data display with pagination, empty text, long-cell wrapping, and horizontal
  table scroll where the data contract requires it.
- `ProForm`: account login/password/profile forms, adapter settings, AI model
  settings, Knowledge Source settings, locale control, and account creation.
- `DrawerForm`: Adapter creation; `ModalForm`: Credential, Package Source, and
  password-reset flows.
- Official antd primitives remain responsible for `Empty`, `List`, `Select`,
  `Input`, `Radio`, `Modal`/`Drawer` surfaces, tags, alerts, and accessible
  actions. Existing Workbench/Monaco/log/Diff/Candidate/attachment behavior was
  not redesigned.
