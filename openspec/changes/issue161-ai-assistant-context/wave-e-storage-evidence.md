# #151 storage checkpoint (task 5.1)

Migration `0045_issue151_ai_conversations` adds only `ai_conversations` and
`ai_conversation_messages`; it revises `0044_issue161_cache_admin` and does
not alter existing business tables. Account ownership requires a user ID and
deployment ownership forbids one. Conversation deletion, Adapter deletion,
and account-user deletion cascade only the corresponding opt-in conversation
rows. A deployment conversation survives deletion of an account owner. The
downgrade refuses while any conversation exists; after explicit deletion it
removes the two new tables while retaining old business rows.

The two-table turn representation is one stable user row and at most one
assistant row per `turn_id`. The user row holds the *current* generation,
idempotency UUID, keyed request HMAC (32 binary bytes), and request status.
The assistant row occupies a stable sequence slot and holds only the latest
successful visible reply, its generation/source revision, bounded Candidate
JSON (validated by the later service), and response metadata needed for a
lost-response retry.
Successful regeneration must replace that assistant row in one transaction;
failure leaves the old reply intact. A late old-generation/key/HMAC request
must conflict through a service-side conditional update, not create another
turn or overwrite the row. The database has unique sequence, turn/role and
current idempotency-key indexes to support that behavior; the CAS transaction
is intentionally deferred to tasks 4.3/5.2.

The SQL checks and model constants cap a conversation at 512 message sequence
slots and 90 days from creation. Each visible message is at most 16 KiB UTF-8;
state is 32 KiB; summary 16 KiB; full covered-range source-revision snapshot
32 KiB; response metadata 16 KiB; Candidate 128 KiB. JSON fields have bounded
top-level key allowlists; later service code must strictly validate their
contents, enforce the same bounds before write, and never persist raw
attachments, credentials, full request/Working Copy snapshots, tool payloads,
or Provider responses. `summary_valid` gates use of a saved summary; its
source-revision array covers **every** message from `summary_covered_from`
through `summary_covered_through`, including uncited replies. Expired rows
remain unreadable once `expires_at <= now()` even before cleanup; later API
queries must enforce that predicate. Clear must delete message rows and reset
summary/state under the conversation lock; delete and expiry cleanup hard
delete the opt-in conversation row. No cleanup job or API is installed here.

On an isolated PostgreSQL 16 container, the migration test upgraded an actual
0044 database to the unique head 0045. Row digests for preexisting
Adapter, AdapterVersion and User rows, plus their column sets, were unchanged.
The test inspected new columns, checks and indexes; parsed the default state
as `ConversationState`; exercised valid account/deployment rows and bounded
reply replay; rejected owner mismatch, retention over 90 days, forbidden JSON
keys, missing source revisions, oversized message/Candidate and short HMAC;
verified unique turn identity/current idempotency key, stale-generation
conditional update returning zero rows, account/conversation delete cascades,
and downgrade refusal with retained history. A selected existing Assist API
test also passed on a fresh-head database. Together with the pure conversation
contract tests: 18 passed.
`ruff check` and `mypy` on the new model passed. The only warning was the
existing third-party Starlette deprecation. No new conversation API, runtime
session use or live Provider behavior was tested in this checkpoint.
