# Issue #117 Batch 5 evidence

- `DISPATCH_ID`: `issue117-b5-editor-20260824-r1`
- `DELIVERY_MODE`: `LOCAL_FAST`
- Scope: OpenSpec tasks `5.1`–`5.3` only (editor configuration collapse and Monaco maximize/restore).
- Base / merge-base: `666084cd40b18d131f59716843f4f1135ff43b9d`
- UI baseline: React 19, Vite, `antd 5.29.3`, `@ant-design/pro-components 2.8.10`.
- All fixture values are anonymous; no real provider, credential, Token, or Secret was used.

## Primary AO Browser evidence

The primary evidence was collected with the session-owned AO Browser panel, not a
host/in-app browser connector. The panel does not provide a viewport control (the
captured panel was `359x851`), so the screenshots below prove live interaction and
accessibility while the auxiliary Chromium matrix proves the required widths.

Screenshots are relative repository paths:

- `ao-browser/zh-CN-initial.png`: both panels default collapsed.
- `ao-browser/zh-CN-independent-expanded.png`: dependency panel interaction and independent binding expansion.
- `ao-browser/zh-CN-reopened-values.png`: dependency `httpx==0.28.0` and binding `RENAMED_TOKEN` preserved after close/reopen.
- `ao-browser/zh-CN-maximized-edited.png`: accessible restore control while editing in maximized Monaco.
- `ao-browser/zh-CN-restored-dirty.png`: Escape restore, Save remains available, and the dirty working copy is retained.
- `ao-browser/en-initial.png`, `en-independent-expanded.png`, `en-reopened-values.png`, `en-maximized.png`, `en-restored-dirty-escape.png`: the same English paths.

The AO Browser snapshots demonstrated:

- accessible localized maximize/restore names: `最大化代码编辑器` / `恢复代码编辑器布局` and `Maximize code editor` / `Restore code editor layout`;
- independent Collapse buttons, keyboard-focusable maximize/restore, and `Escape` restoration;
- edit while maximized, dirty Save state, and preservation of dependency/binding values;
- no page errors (`ao-browser/page-errors.json`) and no `console` level `error` (`ao-browser/console.json` contains only Vite/React/i18next informational messages);
- sanitized metadata-only request capture (`ao-browser/network-report.json`): `19` requests, all `GET`, no non-success responses, and `lifecycle_requests: []`. Request/response bodies and headers were not archived.

## Auxiliary Chromium matrix

The scoped fixture is exercised by:

```text
npm run test:browser -- tests/e2e/issue117-b5-editor.spec.ts
```

The run covers `zh-CN` and `en` at `1280`, `1440`, `1680`, and `1920` pixels
(`8 passed`). `auxiliary-matrix/browser-report.json` records each case and
contains no absolute filesystem paths.

`auxiliary-matrix/assertions.json` is the sanitized post-run verification of
all eight records: all five layout fields match exactly across maximize,
button restore, and `Escape`; lifecycle, console, page, unknown-request, and
overflow assertions are all zero/false.

For every case the test automatically reads the five layout fields from the
live editor region:

1. selection start line;
2. selection start column;
3. selection end line;
4. selection end column;
5. top visible line.

It records the state before maximize, after maximize, before button restore,
after button restore, before `Escape`, and after `Escape`, then asserts exact
object equality for each transition. The report also records:

- `lifecycle_requests: []` for all non-`GET` Save/Run/Revision/Credential,
  execution, schedule, and webhook paths;
- empty `unknown_requests`, `console_errors`, and `page_errors`;
- `horizontal: false` and `vertical: false` in normal, maximized, and restored
  layouts;
- real Monaco keyboard cursor movement, edit while maximized, localized
  accessible button names, focus after each layout transition, and `Escape`.

The matrix fixture uses deterministic code, dependency, and metadata-only
credential values. It never submits Save, Run, Revision, or Credential binding
mutations during layout actions. Screenshots are under
`auxiliary-matrix/browser/` and are intentionally separate from the AO Browser
screenshots above.

## Privacy and path checks

The evidence set contains only relative artifact references and anonymous
fixture values. Before Candidate creation, changed source/evidence files were
scanned for machine home/temp/volume path roots and common Bearer, provider
token, AWS key, and private-key markers; all scans returned zero findings.
`auxiliary-matrix/privacy-scan.json` records the sanitized result.

`FIXTURE_TOKEN` / `fixture-*` are anonymous test placeholders, not findings.
No secret material, real credential fields, browser cookies, request bodies, or
absolute local paths were archived.
