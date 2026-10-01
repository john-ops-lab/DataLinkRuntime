# #151 summary/state contract checkpoint (tasks 3.3, 4.1)

`conversation_summary` builds a two-message, tools-disabled request containing
only the prior valid summary, a contiguous pending message range, and its
source/coverage data. It sends that request through the existing Provider
transport with `purpose=summary` context budgeting and a deadline passed from
the enclosing Assist. The strict `SummarySnapshot` output has version, text,
source references with message revisions, and a coverage cursor. Tool calls,
Candidate fields, missing/unknown/stale sources, and out-of-range cursors are
rejected. Budget, deadline, Provider, and validation failures all return the
previous snapshot unchanged; this module does not persist or advance a cursor.

`ConversationState` stores bounded, sourced facts by explicit goal, explicit
constraint, confirmed decision, inference, pending task, and unresolved
question, with a confirmation level checked against each kind. User-origin
facts cannot be forged from an assistant source;
inferences are marked as assistant-origin. An explicit later user revision can
revoke a fact while retaining its origin and revocation history. The state
schema has no Working Copy or Candidate authority; those remain with the
current request. A summary's cited `sources` list is provenance only. Task 4.2
must conservatively invalidate using the entire covered range and each covered
message's revision, including replies the model did not cite.

Validation: `ruff check` and `mypy` on the two new source modules passed;
`pytest -q tests/test_ai_conversation_contract.py` passed all 15 deterministic
tests (one third-party Starlette deprecation warning). Cases cover dedicated
input shape, shared remaining-time timeout, no-tool transport, invalid tool
calls/Candidate/missing or unknown source/stale source revision/cursor, range
gaps, budget/deadline refusal, Provider timeout retention, early constraint revocation, inference
separation, and refusal of code authority fields. No database, browser, or
live Provider behavior is claimed for this checkpoint.
