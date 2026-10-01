# #151 tasks 4.3 and 5.2: durable Assist and owner-scoped API

Opt-in `session_id` branches Assist to a short-transaction durable turn service;
requests without it keep the existing Assist path. Create/list/read/clear/delete
API checks Adapter edit access and account or deployment owner on every request.
The session read response includes its current revision and visible message rows.

For a new turn or explicit regeneration, the browser supplies
`expected_session_revision`, stable `turn_id`, `expected_generation`, and a
per-attempt `idempotency_key`. The service locks the conversation, checks the
revision, generation and full-request HMAC, and reserves one user row. A
same-key replay checks the frozen request first, so a lost response can return
the committed response despite the revision increment. Clear advances the
revision and deletes rows; an old frozen request cannot recreate them.
Regeneration updates the existing turn and replaces its assistant slot only
after CAS. Pending concurrent replays and late results are rejected. A failed
turn remains visible, and continuation fills its assistant slot with a visible
failure placeholder. Summary rollup runs after the new turn reservation and
shares its deadline; successful rollup advances this turn's CAS baseline only
when its pending row, key, and generation remain current.

Persisted history supplies the Provider context. A valid summary and active
state enter as lower-priority background. Uncovered history must fit in full;
otherwise the request returns `ai_session_context_incomplete`, retaining all
original rows. The service rejects gaps rather than silently skipping history.
Only visible user/assistant text, bounded response metadata, and a code-only
Candidate are saved. Provider compatibility echoes of Working Copy
`requirements` and `runtime_config` are cleared in the first response,
persistence, and same-key replay.

Verified against isolated PostgreSQL 16 and Fake Provider:

- `tests/test_ai_session_turn.py`: same-key replay/conflict, pending concurrency,
  generation/late-result CAS, clear-and-replay rejection, failure placeholder,
  private configuration exclusion from `candidate_json`, summary budget failure,
  successful summary prompt wiring and invalidation, and permission loss after
  reservation.
- `tests/test_ai_sessions_api.py`: two account owners, deployment owner, token
  rotation, ACL revocation, forged IDs, expiry, clear and delete.
- Relevant AI regression selection: 234 passed; Ruff and mypy passed on changed
  Backend modules; `openspec validate issue161-ai-assistant-context --strict`
  passed.

This is Backend/API delivery only. It does not claim Web session UI, live
Provider acceptance, PR creation, or local-preview deployment.
