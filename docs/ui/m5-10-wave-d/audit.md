# M5.10 Wave D browser audit

## Scope

This evidence covers only the Workbench and AI Assistant display/integration
surfaces in Wave D: Monaco/editor toolbar, Workbench runtime/history/live-log
panes, standard icon actions, tooltips, keyboard maximize/restore, Markdown/code
and copy, Candidate/Diff/Apply, attachments, tool-call success, loading/error,
read-only permission state, Adapter switching, and horizontal overflow.

Wave E and whole-site cleanup/audit are intentionally out of scope.

## Deterministic fixture boundary

`m5-10-wave-d-workbench-ai.spec.ts` mocks every relevant Control API request with
in-memory fixture data. The AI response is a local `fixture` provider response;
no model service or real provider credential is used. The fixture response includes
Markdown/code, a sanitized `search_knowledge` success summary, a Candidate, and a
delayed response so the loading state is observable. A separate fixture returns a
stable 503 error for the error state, and account read-only fixtures exercise the
permission boundary.

The existing assistant-ui External Store Runtime and DLR-owned message/Candidate,
attachment, context, request-snapshot, and Adapter-isolation state remain in use.
Maximize/restore is exercised in place and Escape restores focus to the layout
control. Switching to the second fixture Adapter verifies that old AI messages do
not cross the Adapter boundary.

## Matrix and checkpoints

- Locales: `zh-CN`, `en`.
- Viewports: 1280, 1440, 1680, 1920 px, height 900 px.
- Admin scenario: Workbench/editor, run → live log, log selection → AI context,
  live-log maximize/restore/Escape, history/detail log maximize/restore, draft
  preservation across AI maximize/restore, attachment, Markdown/code/copy,
  tool-call success, Candidate/Diff/Apply, Adapter switch isolation.
- Error scenario: AI loading and localized request error.
- Read-only scenario: read-only notice, disabled AI entry, editor accessible name,
  and responsive layout.
- Every record checks page errors, unexpected console errors, unknown API requests,
  and `document/body.scrollWidth <= innerWidth`.

See `browser-report.json` for reproducible per-record state and the PNGs in
`browser/` for the archived screenshots.
