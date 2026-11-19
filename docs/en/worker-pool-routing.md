# Worker Pool routing

Use a Pool to prepare a stopped Adapter's route. Production runs on fixed Workers. A new request that resolves to a Pool is refused before it creates execution or dispatch responsibility (HTTP 409, `pool_routing_not_enabled` at the execution gate). An authorized retry of an existing idempotent request reads back its original responsibility under the existing endpoint contract.

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
