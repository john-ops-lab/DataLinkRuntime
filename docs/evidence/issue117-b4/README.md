# Issue #117 Batch 4 evidence

This directory records the Batch 4 credential-binding role-hint verification for
the local anonymous fixture only. It contains no provider credentials, Secret
values, request bodies, cookies, or machine-specific absolute paths.

## Scope

- Platform admin keeps the existing hint and `System Settings → Credentials`
  entry.
- Non-admin owner receives the exact zh-CN and en role hint and no settings
  entry.
- Non-owner/read-only user receives metadata-only bindings and no add/save
  controls. Backend permission behavior remains covered by the existing
  Credential and Adapter ACL contracts plus the Batch 4 regression fixture.

## AO Browser evidence

The current Worker AO Browser used real local sessions from the fixture auth
entry points. `ao-*-snapshot.json` records the rendered role, locale, exact
copy, metadata-only binding row, and link/control visibility. The owner and
reader keyboard-focus records were captured without activating a mutation.
The archived console/error files contain no page errors; the owner network
record is metadata-only.

- `ao-admin-zh.*`, `ao-admin-en.*`: platform-admin Token session, both locales,
  including the existing settings entry.
- `ao-owner-zh.*`, `ao-owner-en.*`: non-admin Adapter owner, both locales,
  exact role hint, no settings entry, binding controls remain available.
- `ao-reader-zh.*`: non-owner/read-only user, metadata-only binding row and no
  add/save controls.

## Playwright/Chromium responsive evidence

`playwright-browser-matrix.json` and `matrix/` contain the same local fixture
checked with account-admin and account-owner sessions at 1280, 1440, 1680,
and 1920 pixels in zh-CN and en (16 role/locale/width cases). The matrix
asserts exact copy, link visibility, binding-tab selection, keyboard focus,
absence of Secret-like values, console/page/request failures, and horizontal
overflow. All 16 cases passed; no console errors, page errors, request
failures, bad responses, horizontal overflow, or Secret-like values were
observed. Vertical content dimensions are recorded per case.

## Automated verification

- `cd web && npm run lint && npm run typecheck`
- `cd web && npm run test` — 30 files, 337 tests passed
- `cd web && npm run build`
- `cd backend && uv run --frozen ruff check .`
- `cd backend && uv run --frozen mypy`
- `cd backend && DATABASE_URL=... uv run --frozen --project . pytest -q` — 671
  passed
- Credential-focused backend regression: 21 passed
- Ant Design CLI lint for `CredentialBindingsEditor.tsx` — 0 issues

The local fixture database, auth redirect, backend, and Vite processes are
temporary test infrastructure and are not part of this evidence tree.
