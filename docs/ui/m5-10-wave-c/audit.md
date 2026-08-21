# M5.10 Wave C UI audit

## Scope

This Wave standardizes data display, filtering, and forms on reachable Wave B
surfaces only. Routes, API/session/i18n behavior, assistant-ui, and the Issue
#90 identity, role, owner, ACL, CSRF, and webhook contracts remain unchanged.
No Wave D/E Workbench, AI Assistant, Monaco, live log, Diff, Candidate, or
attachment behavior was redesigned.

## Surface coverage

| Surface | Wave C treatment | Contract boundary |
| --- | --- | --- |
| Adapter Catalog | `QueryFilter` for search/type/status, `Empty`, `DrawerForm` for create, long-label responsive CSS | Existing adapter create/select API; no new endpoint |
| Adapter settings and permissions | `ProForm` metadata form and official `List` for grants | Existing owner/edit/read and ACL endpoints; read-only access stays read-only |
| System Settings | `QueryFilter` + `ProTable` for credentials/package sources/knowledge bases; `ModalForm` for credential/package-source forms; `ProForm` for Knowledge Source, locale, and AI settings | Secret values stay write-only/masked; existing credential, package, knowledge, and AI APIs |
| User Management | `QueryFilter` + paginated `ProTable`, `ProForm` create, `ModalForm` reset, row selection and enable/disable bulk actions | Bulk actions are sequential calls to the existing one-user `PATCH`; no bulk backend route was invented |
| Account entry/profile/password | `ProForm` forms with existing test IDs and account-session behavior | HttpOnly/session and CSRF handling unchanged |

## State and accessibility checks

The Playwright evidence in `browser-report.json` and `browser/` covers:

- `zh-CN` and `en` at 1280, 1440, 1680, and 1920 CSS pixels;
- long Chinese/English labels, search/select/filter controls, table/list
  pagination, row selection/bulk action affordances, and modal/drawer form
  focus/overflow;
- admin, owner, and read-only account access, including permission-denied and
  disabled controls;
- empty, loading, API-error, and unconfigured/disabled states;
- keyboard focus progression, accessible names, console/page errors, unknown
  requests, and document/body horizontal overflow.

Expected account bootstrap `401` responses are recorded separately as
`expected_console_errors`; they are not hidden from the audit. Other console or
page errors and all unknown requests fail the browser test.

## Evidence schema

`browser-report.json` records the exact viewport, locale, scenario, screenshot,
overflow widths, console/page errors, expected errors, unknown requests, and
visible state flags for each run. Screenshots are deterministic fixture runs;
they use no real credentials and no production/destructive operations.
