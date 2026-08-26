# Issue #127 A3 Wave A evidence

- `source_candidate`: `a910b832dbea6ff330f9440c7a0ce412ffb99dbe`
- `scope`: OpenSpec tasks `4.1`–`4.4` only
- `product_code_changed`: `false`
- `verdict`: all A3 gates PASS; B0+ not run

## Gate receipt

| Task | Result | Evidence |
| --- | --- | --- |
| 4.1 migration/head/idempotence | PASS | `migration.json` |
| 4.2 legacy/new compatibility and rollback | PASS | `compatibility.json` |
| 4.3 Compose runtime, scheduler and capability gate | PASS | `compose.json` |
| 4.4 Wave A regression, static, browser and scans | PASS | `backend-regression.json`, `web-gates.json`, `browser-matrix.json`, `ao-browser.json`, `scans.json` |

## Browser boundary

AO Browser was used for the live page observation. The anonymous login click remained on the login page under the known AO interaction limitation; this is not recorded as a product PASS or FAIL. Its metadata-only network capture was stopped and cleared (`request_count=0`); AO errors were empty and console had only one `i18next` info entry. The acceptance matrix is the repository Playwright/Chromium auxiliary run: `zh-CN/en × 1280/1920`, four cards, flag-off, run-now, JSON top-level values, revision conflict, request metadata and overflow.

## Diagnostic transparency

The first full Python-only container diagnostic reported `792 passed, 15 failed`; it is retained as a non-green diagnostic with all 15 reasons and classifications in `backend-regression.json`. The fixed runner lacked the repository root and Node/Java runtimes, and three older tests have current-head/legacy-schema expectations. Correct host/Compose environment replacement runs passed and are listed in the same file. No A3 product failure was found.

## Fixture security correction

The scoped Compose Postgres log directory briefly used mode `0777` during fixture startup troubleshooting. The pinned image UID/GID was `70:70`; Colima bind mapping exposed `0:0` in the container and rejected `chown`. The directory was corrected to `0733` only, restarted, and passed the Postgres write check and real `SELECT 1`. The temporary mode mistake and final mode are recorded in `compose.json`; no product or `.gitignore` change was made.

## Evidence hygiene

All retained records use repository-relative paths and sanitized metadata. Raw headers, bodies, tokens, secrets, provider text, local dependency output, raw Compose logs and AO network payloads are not archived. Raw ignored browser output, failed trace and temporary dependency links are removed during final cleanup; `cleanup.json` records exact scoped resources.
