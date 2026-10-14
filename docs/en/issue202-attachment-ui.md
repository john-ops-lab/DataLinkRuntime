# Prepared AI attachments and frontend lifecycle (#202)

The panel retains assistant-ui, temporary page-local conversation, Candidate → Diff → explicit Apply, and manual Save/Test/Run. File selection follows the server's formats and size limits. It never guesses visual capability from a Provider brand or switches the selected model automatically.

## Preparation and coverage

`GET /api/adapters/{id}/ai/attachment-capabilities` returns metadata within the Adapter edit scope. `selected` describes the effective model/endpoint/protocol capability; `providers` is only a catalog/declaration list. When `prepared_attachments_enabled=true`, selecting a file starts `POST /api/adapters/{id}/ai/attachments` with `{scope_id, attachment}`. The attachment retains filename/content_type/data_base64. Preparing, partial success, failure and expiry are distinct states; partial success never claims complete parsing.

Safe metadata includes attachment_ref, sha256, string representation_revision, filename/content_type/size_bytes, ready/partial status, original 1-based pages, sheets, coverage, stable warnings and actual expires_at. The directory retains blank-page numbering, failed/omitted pages, sheet dimensions/print areas and missing-formula-cache warnings. Unknown warnings use a general coverage message rather than an internal code. The scrollable directory shows up to 64 pages and 32 sheets; remaining sources stay available through bounded server tools. A directory or prepared status does not mean the model has read the entire file.

Unavailable conversion or missing fields on an older Control preserve inline attachment compatibility, with an explicit bounded-text fallback explanation for PDF/Office. Unknown visual capability and administrator-declared custom capability have distinct explanations. Only a plain missing-route 404 permits fallback from the Adapter capability route to the legacy global route; access and resource domain errors never do.

## Freezing and release

Each mounted account/Adapter scope uses an in-memory UUID. Sending freezes attachment_scope_id and attachment_ref/sha256/representation_revision, plus a directory copy in the existing round snapshot. File contents/base64 are neither rendered in chat nor persisted as conversation history. Prepared Assist requests carry refs only; legacy inline behavior remains compatible.

The resource registry tracks composer, visible frozen rounds and active requests, without creating another history/Candidate store. Composer removal releases a ref only when no frozen or active reference protects it. Retry/Regenerate first validate the original via `GET /api/adapters/{id}/ai/attachments/{ref}?scope_id=...`. They do not read a new composer file or silently re-prepare a replacement. Expiry or a changed representation requires re-upload and a new round.

Removing a preparing row aborts its request. A late result that still supplies a ref triggers a release attempt and cannot enter another scope. Account/Adapter changes and unmount abort requests and initiate `DELETE /api/adapters/{id}/ai/attachments/scopes/{scope_id}`. Collapse/reopen on the same page keeps the scope. React StrictMode effect replay does not close a still-mounted resource scope.

Actual pagehide initiates a keepalive scope release. Delivery is best-effort: initiating a browser request and completing server cleanup require separate evidence. Server TTL, ownership, active pin/refcount and process/resource cleanup remain authoritative. A frontend AbortSignal or HTTP 200 does not prove that a conversion subprocess stopped.

## Validation boundary

Node24 tests cover frozen identities, expiry, changed representations, cancellation/late results, StrictMode, access-error behavior, unknown warnings and bilingual coverage. Isolated Playwright uses synthetic APIs/files to check refs-only requests, frozen Retry, individual release, expired references stopping before the model, actual pagehide release attempts and an empty conversation after refresh. Missing unload receipts are not reported as successful cleanup.

Real image/PDF/XLSX/XLS/DOCX conversion, Vision models, page/sheet tools and budgets, ownership, TTL and actual asset/process cleanup remain integration acceptance on the precise SHA. Isolated UI regressions do not replace those business results.

Validated on Node 24: targeted session/API/panel tests 56 passed; full Web suite 55 files / 713 tests passed (117.23s); typecheck, lint and production build passed; AntD CLI 6.6.1 against 5.29.3 reported zero issues for the changed panel. Bilingual isolated Playwright: 4 passed (13.5s). The first full run found an old global-only capability fixture; it was corrected to match the scoped contract and the final full suite passed.
