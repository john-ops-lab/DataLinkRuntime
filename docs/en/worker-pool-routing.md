# Worker Pool routing

Use a Pool to prepare a stopped Adapter's route. Before the explicit 0093 cutover, production runs on fixed Workers. In that mode a new request that resolves to a Pool is refused before it creates execution or dispatch responsibility (HTTP 409, `pool_routing_not_enabled` at the execution gate). An authorized retry of an existing idempotent request reads back its original responsibility under the existing endpoint contract.

## Configure a route

An administrator registers an immutable environment at `/api/worker-environments`, creates a rule at `/api/worker-pools`, and explicitly grants membership to a Worker identity. The rule binds an environment class, an equality selector on administrator-issued labels, and a placement scope. Changing the rule creates a revision. Member grants have separate placement, material and credential scopes. A member preview without independently verified environment evidence reports an unknown environment. It cannot establish available capacity.

An explicit Adapter route update uses the current `expected_route_version`. Pool administration uses `expected_version`. Refresh after a version conflict. A fixed route retains its Worker ID and has no fallback. A Pool draft binds the exact Pool and revision and returns null for `runtime_worker_id`. A conflicting legacy ID and explicit route is rejected with 422. Cloning either a fixed or Pool Adapter produces a stopped, unbound copy, even in the same deployment. Explicitly bind the copy. On template use or import, explicitly rebind nonportable references before enabling. A matching Pool name grants no authority.

## Read the binding source

| Operation | Source and retained facts |
| --- | --- |
| Manual, schedule, webhook, initial subscription activation | `adapter` — binding at acceptance or activation. |
| Single T2 or initial business batch | `activation` — the original activation, including when the Adapter has changed. |
| Automatic retry | Reuses the original Execution and frozen route. If the historical Worker cannot be established, retain the outstanding responsibility. Retry and new Outbox creation cannot substitute a mutable target. |
| Event replay, original or repair | `original` or `repair` — retain original routing. Repair changes only the permitted Revision. |
| Explicit current-config event replay; ordinary Execution replay | `current-config` — current binding. Event replay retains its stopped/no-outstanding-work guards. |
| Dependency check and its replay | `dependency-check` — replay requires the original fixed Worker. |
| Pre-upgrade history | `legacy` where a fixed target was recorded; unproved environment stays `unknown`. |

Keep the configured route, current dispatch target and actual Attempt Worker distinct. Existing ordering, failure barriers and replay restrictions continue to apply, including closed Kubernetes completion. Portable assets start stopped.

## Offline upgrade and rollout

1. Back up the database, original volumes and private declaration/intent files. Stop Control writers and drain and reconcile the original deployment per the fixed Worker maintenance procedure. Preserve its authority, epoch, identities, receipts and image digests. See [fixed Worker operations](remote-fixed-worker.md).
2. Use the offline owner path from `0090_issue129_worker_cutover` to `0091_issue129_pool_routing`. Supported W1 source identifiers are the release commit `347be42970a765fc08557528991134d60c79dcae` and accepted image source `bbfedfa93f37f9c008084085e8bbb7998fba8897`. An original J deployment first follows its supported J→0090 transition. Retain the closed predecessor chain.
3. With the owner connection, run `python -m dlr.control.control_identity_init_v4` in order with subcommands `check`, `apply`, then an independent `observe`. Each takes the same private `--source`, `--target`, `--intent` files and predecessor source/intent pairs in order: previous, root, origin, ancestor, initial, earliest. The new pair is `--earliest-source` and `--earliest-intent`. Resolve an uncertain response by observing the original intent. Resume the runtime role only after `committed_current`.
4. Historical Execution and Activation snapshots use their recorded fixed Worker; a missing historical Execution target stays unclassified. This upgrade enables stopped drafts. Production Pool reception requires the later coordinated cutover, including capacity reconciliation, pre-dispatch reservations, bounded waiting and recovery.

### Capacity ledger upgrade

The supported source for `0091_issue129_pool_routing` is release commit `f536419b59282891cf537e2d8fef65b7e8b92d1c`. Drain the deployment and follow the same offline owner procedure to reach `0092_issue129_worker_capacity`. The full predecessor chain adds `--first-source` and `--first-intent`. Resume fixed Worker service with the runtime role after the independent observation reports `committed_current`.

This version validates capacity and reservations in isolation. Allocatable capacity comes from verified instance limits after system and Agent headroom. Both capacity timestamps must be within 15 seconds. Work awaiting placement pays for one maximum dispatch message. Assignment replaces that charge with the serialized size and creates a 30-second hold. Production Pool acceptance and fixed Worker reservation enforcement require the coordinated pause, drain, capacity reconciliation and recovery cutover.

### Enable placement after the 0093 upgrade

Upgrade from `0092_issue129_worker_capacity` at `36ee472851e2141ce908dcd9a9588c5f6a86f9eb` to `0093_issue129_worker_placement` using the offline owner check/apply/independent observe sequence above. The longest history appends `--oldest-source` and `--oldest-intent`. Resume after `committed_current`. The database starts in `fixed`; operators may continue the guarded legacy path. Enabling reservations is a separate deployment operation.

The 0093 upgrade also updates database validation for business batches. The v4 runtime role and every role it can reach must lack the database `CREATE` and `TEMPORARY` privileges. When the restricted role commits a T2 batch or its member results, the database checks frozen membership, charges and result ownership on each validation. A failed check rolls back that transaction. Deployments that already permit temporary tables can continue using the transaction-local validation cache.

Run each subcommand with `python -m dlr.control.control_identity_init_v4` under the deployment owner and current authority declaration.

1. Read `placement-status`. Before initial approval, disable the reserved Worker. Independently verify its stable identity, instance, cgroup v2 envelope, deployed toolchain, environment class and signer public key. Protect the host signing key outside task-accessible directories and configure `DLR_WORKER_CAPACITY_SIGNING_KEY_FILE`. Run `placement-approve-capacity WORKER_ID ENVIRONMENT_NAME PUBLIC_KEY_HEX` with the 32-byte Ed25519 public key in hex. Member, material and credential grants remain administrator-controlled. Activate the reconciled instance and verify fresh signed capacity.
2. Run `placement-pause`; drain active work, preserve its journals and allow result and cleanup settlement. Run `placement-reconcile` in bounded batches until the old waiting work is accounted for. Conversion supersedes the old dispatch generation. Queued work receives a prepaid waiting intent; retry-wait work retains its backoff. Keep old v3 work free of retroactive expiry. An uncertain physical disposition blocks recovery.
3. Run `placement-enable` and independently read `placement-status` for `reserved`. If blocked, inspect obligations and capacity, then resume the same procedure. An outage or restart cannot revert to legacy claims. Every new Claim needs a current reservation.

Capacity admission uses the verified host envelope after Agent and receive overhead, capped by the Worker’s per-task limits. Held, claimed and reclaiming charges count against that capacity. The scheduler rotates runnable Adapter/Pool heads and scans overdue Executions independently. A preparation failure quarantines the Worker for at least 30 seconds. After that deadline and confirmed cleanup, use the supervised restart procedure on the existing identity and volumes to obtain a new preflight and signed capacity observation. Retain the original physical charge while cleanup is uncertain.

Inspect the configured route, actual Attempt Worker and dispatch generation through `GET /api/executions/{execution_id}`. Read the Worker ledger at `GET /api/workers/{worker_id}/v4/status` and cluster counts at `GET /api/workers/placement-metrics`. At the placement limit an administrator can use `POST /api/executions/{execution_id}/placement-retry` with `expected_generation`. Cancellation uses the existing edit-authorized `POST /api/executions/{execution_id}/cancel`. Retain both action audit records. For in-place recovery, require independently verifiable physical evidence for the original obligation. A retry-wait disposition refers to the old reservation generation.

For D13, use two independent Linux Worker hosts, compatible grants and envelopes, a real Control, PostgreSQL, RabbitMQ and an independent idempotent target. Fault the first host before Claim and measure fault to compatible Claim (≤60 s). Repeat after Claim and measure fault to new fenced Claim (≤180 s, including the original lease and grace). Saturate the pool, add compatible capacity and measure confirmed ready to Claim (≤10 s); repeat with up to ten runnable Adapters. Record first failures, Attempt counts, target-side effects per business key, old-instance rejection and physical ledger settlement. Missing host samples leave 6.6 unverified.
