# Issue #117 Batch 6 AO Browser evidence

This directory records the primary, session-owned AO Browser run for
`DISPATCH_ID=issue117-b6-ai-20260825-r1`. The fixture is anonymous and local;
it does not use a real Secret, Provider credential, or Provider response.

## Primary AO Browser run

- `zh-CN` and `en` were opened in the session-owned AO Browser at the same
  local app origin.
- The AI Assistant note was exposed as a `note` with the complete attachment
  limits and privacy text in its accessible name.
- The attachment control remained an accessible button with the existing
  supported-type name in both locales.
- The real attachment button was activated in both locales. The native chooser
  was safely cancelled with `Escape`, so no local file was selected or read by
  this primary run.
- An anonymous fixture prompt was submitted in each locale. The resulting
  metadata-only capture recorded `POST /api/adapters/1/ai/assist` and the
  adjacent credential-bindings `GET`, both with status `200`.
- AO Browser `errors` returned zero messages. Console output contained only
  development `debug`/`info` entries; no console error was observed.

The AO Browser command surface has no file-system `setInputFiles` primitive.
Actual picker, drag/drop, invalid-type, and remove lifecycle behavior was
therefore exercised in the separate local Chromium fixture matrix below,
without weakening the primary AO Browser accessibility/request check.

See `ao-browser-report.json` for sanitized snapshots and request/console
counts. Request capture was metadata-only and was stopped and cleared after
the run. No raw request/response body is archived.

## Auxiliary Chromium matrix

`../auxiliary-matrix/browser-report.json` is a separate Playwright/Chromium
fixture record for `zh-CN`/`en` × `1280`/`1440`/`1680`/`1920`. It performs:

- picker upload, valid drag/drop, unsupported-type rejection, and remove;
- one-line/`nowrap`/`scrollWidth <= clientWidth` assertions;
- complete accessible limit/privacy text assertions;
- page horizontal/vertical overflow checks;
- one fixture assist request, no unknown paths, and zero console/page errors.

The eight screenshots under `../auxiliary-matrix/browser/` are auxiliary
Chromium evidence, not screenshots from the AO Browser panel.
