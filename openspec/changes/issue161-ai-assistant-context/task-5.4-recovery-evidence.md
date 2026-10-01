# #151 task 5.4: recovery checkpoint

The Chinese and English product and architecture documents now describe the
explicit saved-session boundary, owner and Adapter access, 30-day retention,
current Working Copy authority, lower-priority summary, and the limits of
recovered materials. The previous blanket claim that all conversations remain
memory-only was removed.

An isolated PostgreSQL 16 test created a saved session through the API, sent a
turn with a one-request text attachment, created a fresh Control application
instance using the same database, read the session, and continued with a
different current Working Copy. The new app recovered only visible user and
assistant text. Reading history made no Provider call; continuing used the new
Working Copy and earlier visible messages, without the attachment body. The
attachment body was absent from persisted message text. The focused test
`test_new_app_recovers_visible_session_without_temporary_material_or_old_code`
passed. The task-owned PostgreSQL container was stopped and removed afterward.

This verifies application recreation over a persistent database, not a full
deployed Control process restart. The latter, real browser recovery, expiry and
deletion behavior, and no tool replay during UI restoration remain for local
preview acceptance before task 5.4 is checked complete.
