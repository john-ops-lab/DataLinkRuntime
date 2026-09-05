# Issue #130 Reliable Runtime Migration, Cutover, and Failure Handling

> Historical design record, not the current deployment runbook. The unified
> execution runtime has removed the legacy/canary/Cutover APIs, switches, and
> workflows. Do not run these historical commands; use
> [Sandbox Deployment and Troubleshooting](issue130-sandbox-deployment.md).

[简体中文](../zh-CN/issue130-reliable-runtime-migrations.md) · **English**

This is the deployment/API runbook for Reliable Runtime. Database URLs,
credentials, evidence IDs, and host paths below are placeholders; never put real
values in documentation, shell history, Issues, or ordinary logs. Final Cutover is
an explicit administrator operation. Neither `alembic upgrade head` nor ordinary
Compose startup performs it automatically.

## Schema and Safe Defaults

`0030_issue130_reliable_runtime` follows
`0029_issue127_c0_exec_lease` and adds the Queue/Outbox/Admission schema.
`0031_issue130_b2_runtime` adds Attempt/Slot/Incident/Hold and the v3 runtime schema.
Historical Executions are deterministically backfilled as
`dispatch_backend=legacy`.

An ordinary upgrade runs additive migrations only:

Additive does not mean lock-free or safe for an online rolling upgrade. Revision
`0030` backfills the full `executions` table, applies non-null and validating
constraints, and rebuilds a unique index without `CONCURRENTLY`. Production
databases with existing rows require a maintenance window that stops Execution
writes, plus table-size-specific timing, lock-wait, backup, and restore checks.
Do not run the command below as a normal rolling upgrade while writes continue.

```sh
docker compose run --rm control alembic upgrade head
docker compose ps
```

Defaults remain fail closed after upgrade:

```text
DLR_RABBITMQ_EXECUTION_ENABLED=false
DLR_MIN_WORKER_PROTOCOL_VERSION=1
DLR_LEGACY_EXECUTION_CLAIM_ENABLED=true
DLR_CUTOVER_BACKUP_RESTORE_GATE_PASSED=false
DLR_CUTOVER_SANDBOX_GATE_PASSED=false
DLR_CUTOVER_SLOT_GATE_PASSED=false
```

Retiring `uq_executions_active_adapter` is a separate, explicit, guarded Cutover
operation, not a new Alembic revision. The same `0031_issue130_b2_runtime` revision
may therefore have the old index present or be post-Cutover. Read inventory and
invariant results; never infer operational state from the revision alone.

## Administrator API

Every endpoint requires `Authorization: Bearer <admin-token>`. The host and token in
these examples are intentionally fake:

| Method and path | Read-only | Purpose |
|---|---:|---|
| `GET /api/admin/reliable-runtime/inventory` | Yes | Revision, backend/status counts, Worker protocol/Sandbox, Rabbit/Outbox, Cutover gates, and old-index state |
| `POST /api/admin/reliable-runtime/migration/dry-run` | Yes | Computes the legacy pending/running boundary without database writes |
| `POST /api/admin/reliable-runtime/migration/legacy-running-drain` | Yes | Asserts that legacy running is zero; never converts a running row |
| `POST /api/admin/reliable-runtime/migration/legacy-pending` | No | Idempotently converts `limit=1..1000` pending rows and atomically creates Admission/Outbox |
| `GET /api/admin/reliable-runtime/cutover/preflight` | Yes | Separately reports migration `status/blockers` and the `index_retirement` gate |
| `POST /api/admin/reliable-runtime/cutover/retire-legacy-index` | No | Retires the old active index under a lock and second check; safe to repeat |
| `GET /api/admin/reliable-runtime/cutover/invariants` | Yes | Checks structural DB invariants, all-Worker v3/Sandbox readiness, and the Infrastructure DLQ |

Read-only examples:

```sh
curl -fsS \
  -H 'Authorization: Bearer <admin-token>' \
  https://dlr.example.invalid/api/admin/reliable-runtime/inventory

curl -fsS \
  -H 'Authorization: Bearer <admin-token>' \
  https://dlr.example.invalid/api/admin/reliable-runtime/cutover/preflight
```

Index retirement requires the literal confirmation, exact schema revision returned
by preflight, and the identifier of this run's verified backup/restore evidence:

```sh
curl -fsS -X POST \
  -H 'Authorization: Bearer <admin-token>' \
  -H 'Content-Type: application/json' \
  --data '{
    "confirmation": "retire-legacy-active-index",
    "expected_schema_revision": "0031_issue130_b2_runtime",
    "backup_restore_evidence_id": "EXAMPLE_RESTORE_EVIDENCE_ID"
  }' \
  https://dlr.example.invalid/api/admin/reliable-runtime/cutover/retire-legacy-index
```

`changed=true` means that call removed the index. When the index is already absent
under the same schema, a safe repeat returns `changed=false`. Before the first
removal, any unsatisfied gate, Worker, Rabbit, Outbox, DLQ, legacy active, or
structural invariant returns an explicit 409 and leaves the index intact.

## Non-interchangeable Final Cutover

Stop on every failed step; never skip ahead:

1. Freeze deployment changes and record Candidate SHA/tree, Alembic revision, and
   inventory. Take a real database backup, restore it into a separate database, and
   compare the schema and critical counts.
2. Complete the Worker v3 Sandbox Gate in target Linux Compose. It requires an exact
   delegated cgroup v2 subtree, host cgroup namespace, and the full capability
   matrix. See [Sandbox deployment](issue130-sandbox-deployment.md).
3. Call dry-run and legacy-running-drain. Let legacy running finish under its old
   contract; never convert a running row in place.
4. Either drain legacy pending with old Workers or invoke legacy-pending migration
   in batches until inventory reports legacy pending/running as zero. Repetition must
   not create duplicate Outbox rows.
5. Require every continuing Worker to report protocol v3, RabbitMQ capability true,
   isolation preflight passed, and the complete matrix; only then enable ordinary
   RabbitMQ ingress.
6. Smoke Manual, Schedule, Webhook, and all three languages. Every new Execution must
   use `dispatch_backend=rabbitmq`, and legacy Claim must never read it.
7. While the old index still exists, pressure-test Slot and recovery authority: one
   Adapter never exceeds one active Attempt, different Adapters run concurrently,
   and terminal state releases Slot and Admission. Only after PASS, set minimum
   protocol 3 and prove v1/v2 are explicitly rejected.
8. Set all three Cutover attestations and rerun preflight. Invoke the retirement API
   only when migration `status=ready` and `index_retirement.status=ready`, then
   immediately repeat the Slot pressure test.
9. Only after legacy pending/running is zero and the old index is absent, set
   `DLR_LEGACY_EXECUTION_CLAIM_ENABLED=false` and roll the compatible Control. Old
   Claim returns `legacy_claim_disabled`; terminal history remains readable.
10. Run post-Cutover invariants twice. Both results must be `status=passed`,
    `violations=[]`, Infrastructure DLQ ready/unacknowledged both zero, and stable.

`DLR_CUTOVER_*_GATE_PASSED=true` is an operator attestation to external evidence; it
does not replace that evidence. Never set it before the corresponding real test.

## ACK, Single-node Broker, and External Side Effects

The v3 normal order is:

```text
RabbitMQ delivery
→ durable Control Claim commit
→ atomic Worker private-journal persistence
→ ACK
→ Sandbox execute
```

This is **ACK-on-claim**, not ACK-on-completion. If Worker crashes after ACK,
database Attempt Lease/Fencing and Recovery create a new generation; the system does
not rely on the original message remaining in RabbitMQ. A lost Publisher Confirm may
produce duplicate dispatch, but generation, the active-Attempt constraint, and Slot
absorb platform duplicates. Adapter side effects against external systems still need
business idempotency keys.

Default Compose has one RabbitMQ node. A Quorum Queue provides durable semantics on
that node but **not HA**. PostgreSQL Outbox retains accepted responsibility while the
broker is unavailable, and compatible Control Relay publishes it after recovery.

## Rollback Boundary

### Before Cutover

While still additive/dark-launch with legacy Claim and the old index intact, new
RabbitMQ ingress may be closed and legacy traffic may continue. Keep the additive
schema and Relay, and first ensure that existing RabbitMQ Executions are still
drained/repaired by compatible Control/Worker. Turning off a gate never abandons
accepted responsibility.

### After Cutover

Once the old index is retired or legacy Claim is closed, starting an old
Control/Worker is **not** rollback, and simply disabling RabbitMQ ingress is unsafe:
it would route new requests toward a closed legacy path, and configuration validation
fails closed. Correct recovery is:

1. Keep the additive schema and a compatible Control that understands v3 rows.
2. Apply maintenance/rate limiting at the ingress edge instead of rewriting backend
   responsibility.
3. Repair Broker/Worker/Control and continue Relay, Lease Recovery, Retry, and
   Incident/Replay.
4. Repeat inventory and post-Cutover invariants until responsibility and resources
   converge.
5. If a reverse migration is truly required, authorize a separate change that first
   proves no active Attempt/Outbox, completes backup/restore, and receives independent
   audit.

`downgrade()` in revisions `0026` through `0031` is isolated-test cleanup only.
Production rollback must not use `alembic downgrade` as recovery.

## Failure Table

| Symptom | Safe action | Forbidden action |
|---|---|---|
| Preflight is `blocked` | Hold the current phase, resolve each `blocker`, rerun read-only checks | Manually drop the index or invent an attestation |
| RabbitMQ unavailable | Keep compatible Control; observe Outbox pending count/bytes/oldest age; restore the same controlled topology and verify Relay convergence | Delete pending Outbox, rewrite it as legacy, or expose management publicly |
| Outbox exceeds a protection line | Apply maintenance/rate limiting at ingress and restore publisher/Broker headroom first | Create an unbounded queue or bypass Admission |
| Worker offline/crashed | Restore the same fixed v3 Worker and wait for Lease Recovery/new generation | Reroute to an unverified Worker or start v1/v2 against new rows |
| Infrastructure DLQ non-empty | Inspect the matching Incident, fix the permanent cause, use controlled Replay, rerun invariants | Empty the DLQ or delete the Incident just to obtain green status |
| Invariant violation | Stop the next Cutover/release step, preserve safe sample IDs, fix the specific code, rerun twice | Auto-rewrite rows, weaken assertions, or run a schema downgrade |

Logs, evidence, and tickets record only stable IDs, counts, states, and error codes.
Never record RabbitMQ URL userinfo, Claim/Cleanup Tokens, Credential true values,
storage keys, host absolute paths, or user content.

## Old-binary Fail-closed Boundary

Old Control/Worker binaries cannot safely interpret RabbitMQ Executions, Outbox, new
states, or Attempt/Slot. Legacy Claim may read only `dispatch_backend=legacy`;
v1/v2 Workers must explicitly reject a RabbitMQ backend, new state, or unsupported
payload, never silently execute it or rewrite responsibility as legacy.

If compatible Control, actual database state, pending responsibility, Worker
protocol, or Sandbox evidence cannot be confirmed, deployment remains fail closed:
make no next-stage mutation and escalate instead of deleting new tables, weakening
validation, or inventing completion.
