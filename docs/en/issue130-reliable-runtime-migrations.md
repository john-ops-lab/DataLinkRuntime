# Issue #130 Reliable Runtime migration notes

This note defines the Batch 1 additive migration, rollback, and old-binary
boundaries. Database URLs, credentials, and host paths in commands are
placeholders; never put real values in documentation, logs, or shell history.

## Migration paths

`0030_issue130_reliable_runtime` has
`0029_issue127_c0_exec_lease` as its parent. It only expands the schema: it
does not switch RabbitMQ ingress or implement the v3 Claim/Attempt runtime.
Existing Executions are deterministically backfilled as
`dispatch_backend=legacy`; `uq_executions_active_adapter`, the current minimum
protocol, and the legacy Claim remain in place during Batch 1.

Run the real PostgreSQL migration with the current Control deployment:

```sh
docker compose run --rm control alembic upgrade head
docker compose ps
```

Record a database backup and the current Alembic revision before upgrading. An
independent empty PostgreSQL database and a current-main snapshot created at
`0029_issue127_c0_exec_lease` must each run `alembic upgrade head`. The explicit
regression evidence is in
`backend/tests/test_migration_m5_4_3.py`:
`test_fresh_postgresql_upgrade_reaches_issue130_head` and
`test_current_main_0029_snapshot_upgrades_to_issue130_head_and_backfills_legacy`.

## Non-destructive rollback

Rollback is a deployment rollback, not a schema downgrade:

1. Close new RabbitMQ ingress while retaining Outbox, Admission, and accepted
   responsibility.
2. Use a compatible Control that understands the additive schema to drain and
   repair accepted responsibility.
3. Independently audit that pending/running work, Attempts, Slots, Outbox, and
   counters have converged before making the next deployment decision.
4. Keep the new tables, snapshots, audit facts, and legacy columns; rollback
   must not delete data automatically.

The `downgrade()` functions in `0026` through `0030` are isolated-test cleanup
paths. Production rollback **must not** run `alembic downgrade`. Any separately
authorized reverse migration requires backup/restore evidence, proof of no
active Attempt/Outbox responsibility, and a change audit. A pending Outbox in
Batch 1 is not a reason to downgrade and must remain recoverable.

## Old-binary fail-closed boundary

Old Control/Worker binaries cannot safely interpret RabbitMQ Executions,
Outbox rows, or the new status union. The legacy Claim path may read only rows
with `dispatch_backend=legacy`; v1/v2 Workers encountering a RabbitMQ backend,
new status, or unsupported payload must explicitly reject it, never silently
execute it or rewrite it as legacy. Keep the compatible Control in service
during rollback; do not start an old binary against the new rows merely because
it can connect to the database.

If the compatible Control, database revision, pending responsibility, or
protocol distribution cannot be confirmed, deployment remains fail-closed:
stop new RabbitMQ responsibility and escalate instead of deleting new tables,
weakening validation, or attempting a destructive downgrade.
