# #151 task 5.3: Web session implementation checkpoint

The AI panel now offers explicit saved-session creation, listing, selection,
read/reload, clear and delete alongside the original temporary conversation.
It stores only the selected session ID in owner- and Adapter-scoped
`sessionStorage`; visible messages, Candidate data, attachments and tool
details are not written to browser storage. A refresh restores server-owned
visible text only. A close/reopen in the same page reconciles that text with
current in-memory rounds, preserving the still-available Candidate and frozen
retry request. An identity or Adapter switch clears the displayed history and
fences late reads and Assist responses.

Saved-session new turns carry the current session revision from `GET`, a new
stable turn ID, generation zero and a new idempotency key. A failed same-key
retry reuses its exact frozen request, including the old revision and temporary
attachments still held in memory. Explicit Regenerate reads the latest
revision/generation and uses the **current** Working Copy and a new key; it
does not recover old code or temporary material from history. Restored
Candidates are unavailable and never automatically applied. The panel states
that limitation and disables original-request retry when its frozen context
no longer exists. The temporary conversation still uses its original
browser-only request and Regenerate behavior.

The repository's fixed Ant Design 5.29.3 CLI snapshot was queried for Select,
Popconfirm and Button APIs; its component lint reported zero issues. Demo
queries were attempted but timed out, so the implementation used the exact
versioned API metadata and existing project patterns.

Verification:

- `npm run lint`, `npm run build` (including TypeScript) passed.
- Eight related Web suites passed: 98 tests, including nine new behavior
  tests for refresh recovery, frozen retry metadata and attachment body, current Working Copy
  regeneration, close/reopen Candidate retention, clear and owner-change late
  result fencing, failed-turn continuation IDs, stale pre-reservation draft
  recovery, and session lifecycle actions.
- Existing Adapter-switch tests now assert removal of the old display, as
  required by the owner/isolation boundary.

Real Chrome interaction remains for the main task's acceptance step. This
checkpoint does not claim PR, deployment or live Provider acceptance.
