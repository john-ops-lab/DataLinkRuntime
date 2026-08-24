# Issue #117 · Batch 7 Adapter Catalog evidence

## Scope and boundary

- `DISPATCH_ID`: `issue117-b7-catalog-20260825-r1`
- `DELIVERY_MODE`: `LOCAL_FAST`
- Base and parent: `657838a6a1af297889fed7070412252311703767`
- Branch: `ao/datalinkruntime-90/root`
- OpenSpec scope: tasks `7.1`–`7.3` only. Batch 8+ remains unchecked and was not implemented.
- Contract: retain the Catalog title, New, Refresh, Help, Search, type/status filters, list and status behavior; remove the permanent `catalog.overview` rendering; let the search/filter/list region occupy the released space without a replacement explanation block.

## Implementation audit

- `AdapterCatalog.tsx` no longer renders `adapter-catalog-description` or `catalog.overview`.
- The paired `zh-CN` and `en` `catalog.overview` keys were removed symmetrically. A product-source search confirms no remaining `catalog.overview` usage; OpenSpec wording and absence assertions are intentionally retained.
- Catalog header padding was reduced from `10px 12px 8px` to `8px 12px 6px`; the existing filter flex semantics remain unchanged.
- `AdapterCatalog.test.tsx` now asserts overview absence while retaining title/actions/search/filter/list/refresh/help coverage and adds the existing create request/drawer-state assertion.
- `m5-11-wave-v2-adapters.spec.ts` changed only its old overview expectation to `toHaveCount(0)`. Its title, toolbar, actions, search/filter, settings, console/page-error, overflow and unexpected-request assertions were not weakened or removed.

## Automated verification

Commands were run from `web/` unless noted:

- `npm run test -- --run src/components/AdapterCatalog.test.tsx` — PASS, 1 file / 11 tests.
- `npm run lint` — PASS.
- `npm run typecheck` — PASS.
- `npm run test` — PASS, 30 files / 344 tests. Existing unrelated stderr warnings (i18next notice, Ant Design `InputNumber addonAfter` deprecation, and fixture `UNEXPECTED REQUEST` diagnostics) did not fail the run.
- `npm run build` — PASS. Vite emitted its existing large-chunk advisory only.
- `npm run test:browser -- tests/e2e/issue117-b7-catalog.spec.ts` — PASS, 8/8 Chromium cases.
- `openspec validate issue117-manual-test-fixes --type change --strict` — PASS.
- `git diff --check` — PASS before Candidate commit.

## Scoped Playwright/Chromium matrix

`auxiliary-matrix/browser-report.json` contains all eight records and the eight corresponding PNGs:

- Locales: `zh-CN`, `en`
- Viewports: `1280`, `1440`, `1680`, `1920`
- Browser: Chromium `151.0.7922.34`
- Each record: overview count `0`; header-to-toolbar gap `0`; toolbar-to-list gap `0`; document/body `scrollWidth == innerWidth`; console/page errors empty; unknown requests empty; one existing `POST /api/adapters` create request; refresh caused repeated adapter-list GETs.
- Screenshots are taken before the create drawer opens, so the Catalog title, controls, filtered list and compact spacing remain visible in each matrix image.
- Fixture data and route handlers are scoped to this spec. No provider response bodies or real credentials are archived.

## AO Browser session evidence

The session-owned AO Browser was opened with `ao preview http://127.0.0.1:4173` and `ao browser open http://127.0.0.1:4173` against a loopback Vite app plus an anonymous local fixture API.

- `zh-CN`: login by keyboard Enter, initial Catalog snapshot, search, type/status combined filtering, refresh, New form, safe create request and post-create workbench are archived in `ao-browser/`. The initial and filtered snapshots show the title, all required entries, list/status content and no overview text. The Help entry is present and its behavior is covered by the bilingual unit test and the Playwright fixture path; AO Browser click/Enter on the AntD Popover trigger did not expose the Popover text in the session snapshot.
- `en`: initial Catalog and search snapshots are archived, with title, New, Refresh, Help, Search, filters, list and status visible; the 2x4 matrix provides the complete English layout/interaction gate, while the bilingual unit test preserves the Help behavior.
- `zh-CN-network.json` and `en-network.json` contain AO Browser sanitized request metadata only. `zh-CN-console.json` / `en-console.json` contain no error-level console messages; `zh-CN-errors.json` / `en-errors.json` contain no page errors.
- `screenshot-availability.md` records the AO Browser screenshot limitation exactly. The screenshot command produced no structured output or file and the second attempt was stopped with exit `130`; no unavailable screenshot is claimed as evidence. The complete 2x4 screenshots remain in `auxiliary-matrix/browser/`.

## Privacy and cleanup

- Only the explicit anonymous placeholder `FAKE_ADMIN_TOKEN` was entered in the local fixture session; no real token, Secret, cookie, provider credential, raw provider response, or machine data is archived.
- Evidence contains no machine absolute filesystem paths. URLs are loopback fixture URLs and request records are metadata-only.
- Temporary Vite and fixture processes were stopped after AO Browser capture; a process check found no matching `vite --host 127.0.0.1 --port 4173` or `dlr-b7-fixture-server.mjs` process.
- AO Browser network capture was stopped. No Docker container, push, PR, Hosted CI, GitHub Check, Issue, reset, lock deletion, or main merge was performed.
