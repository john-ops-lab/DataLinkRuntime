# M5.10 Wave B UI evidence

Date: 2026-08-21 (Asia/Shanghai)

This directory contains the Wave B-only browser evidence for Issue #100. The
baseline was aligned to `origin/main` at
`1b9cfe375e255967051d2f019d8a52de4a714453` before implementation.

## Contract and versions

- Scope: application shell and shared interaction/state standardization only.
- Preserved: existing routes, API/session behavior, permissions, i18n,
  assistant-ui behavior, and the Issue #90 identity/ACL contract.
- Not entered: catalog search/filter/list/forms, Workbench/AI redesign, or
  later Wave C/D/E work.
- Checked-in Ant Design Skill and pinned project rules were used for the
  official queries and implementation.
- Runtime versions: React 19, `antd@5.29.3`,
  `@ant-design/pro-components@2.8.10`, and
  `@ant-design/v5-patch-for-react-19@1.0.3`.

The pinned official query form was used before editing:

```text
npx --yes @ant-design/cli@6.6.1 --version 5.29.3 <command> --format json
```

Queries covered `ConfigProvider`, `Layout`, `Breadcrumb`, `Menu`, `Button`,
`Tooltip`, `Dropdown`, `Modal`, `Drawer`, `Empty`, `Result`, and `Skeleton`,
including Layout/Menu/Dropdown/Result demos and the 5.29.3 changelog.

## Implemented Wave B behavior

- `ProLayout` (`layout="mix"`) supplies the application shell, fixed sidebar,
  navigation menu, and responsive container; `PageContainer` supplies the
  page title, subtitle, breadcrumb, and content spacing.
- The shared top bar keeps health, Worker, administrator, and account actions
  in one responsive region. Standard icons use `@ant-design/icons`; global
  actions have visible labels, explicit accessible names, and `Tooltip`.
- `Alert`, `Empty`, `Result`, and `Skeleton` cover global errors, catalog and
  workbench empty states, read-only permission feedback, and account bootstrap
  loading. Existing `Dropdown`, `Modal`, and `Drawer` flows remain on the
  official Ant Design primitives.
- Sidebar navigation has an accessible label and disables Workbench until an
  Adapter is selected. Long Chinese/English titles and status copy wrap or
  truncate without horizontal overflow.

## Browser audit

Command run from `web/`:

```text
DLR_WAVE_B_OUTPUT_DIR=/Users/king/.ao/data/worktrees/datalinkruntime/datalinkruntime-66/docs/ui/m5-10-wave-b npm run test:browser -- tests/e2e/m5-10-wave-b-shell.spec.ts
```

The Chromium audit ran 9 Playwright tests and produced 19 records:

- 16 normal shell records: `zh-CN` and `en` × 1280, 1440, 1680, and 1920 ×
  administrator and read-only account paths.
- 3 state records at 1280: loading, empty/disabled, and API error feedback.
- All records have zero unexpected console errors, page errors, unknown API
  requests, and horizontal overflow. Expected fixture responses are retained
  separately in `expected_console_errors`: account bootstrap `401` responses
  for the unauthenticated read-only flow and `503` responses for the explicit
  error-state fixture.
- The audit also verifies the menu focus target is a `menuitem`, navigation and
  action names are available to assistive technology, and read-only feedback is
  visible without changing the ACL behavior.

See [`browser-report.json`](./browser-report.json) for machine-readable
records and [`browser/`](./browser/) for the 19 screenshots.

## Local verification

- `npm run lint`
- `npm run typecheck`
- `npm test -- --run`
- `npm run build`
- Official Ant Design CLI lint: no new findings; only the two existing
  `InputNumber addonAfter` deprecation warnings remain.
- Existing Wave A baseline Playwright scenarios were rerun after the shell
  change; no backend or Compose test was needed because this change does not
  modify backend, API, database, or Compose code.
