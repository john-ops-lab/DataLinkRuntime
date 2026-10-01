# #151 task 4.2: rolling summary and state evidence

`conversation_rollup` selects every persisted visible message before the recent
window boundary in batches of at most eight messages/four complete turns. A
gap, including a missing assistant slot, returns
`ai_summary_original_missing`; callers cannot skip or synthesize it. The
summary call receives only the old valid summary/state and a contiguous pending
range. Its output may propose state additions and revocations with exact quotes
from pending messages. The service validates source revisions, quote substrings,
kind/confirmation/role, active revocation targets, size bounds, and assigns the
state revision. Addition and later revocation in one batch retain separate
service-assigned revisions. The Provider cannot write state directly.

The Provider call runs outside a DB transaction. Persistence locks the
conversation, compares its revision and coverage cursor, then compares
sequence, role, and revision for **every** covered source, including uncited
assistant replies, before updating summary, cursor, source-revision snapshot,
state, and conversation revision together. Rejected, timed-out, or stale
attempts leave the prior valid snapshot unchanged. A covered reply replacement
calls `invalidate_changed_reply` under the same conversation lock and clears
dependent state; the next rollup rebuilds from sequence 1. Task 4.3 must create
a visible failure/abandonment assistant slot before continuing after an
unsuccessful turn, as frozen by the OpenSpec contract.

Verified with Fake Provider and isolated PostgreSQL 16:

- `tests/test_ai_conversation_contract.py` and
  `tests/test_ai_conversation_rollup.py`: 23 passed. Cases include incremental
  cursor, early constraint and later user revocation, uncited covered reply
  replacement, missing slot rejection, invalid evidence, Provider timeout,
  stale conversation revision, changed source revision, a 12-message backlog
  advancing 1–8 then 9–12, and addition/revocation within one batch.
- Ruff and mypy passed on changed source and test modules.

This checkpoint does not claim Assist route wiring (task 4.3), live Provider
acceptance (task 4.4), or API ownership flows (task 5.2).
