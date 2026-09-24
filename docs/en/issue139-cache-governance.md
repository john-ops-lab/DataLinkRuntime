# Issue #139 Worker Cache Governance Operations

This guide describes Worker version-cache policy, safety boundaries, and recovery. It documents the final management contract; the management surface and commands are usable only after the corresponding release is deployed and accepted. A successful build or the presence of code does not mean reclamation is enabled in the current environment.

## Default behavior

Periodic and pressure reclamation are both disabled by default. Workers still reuse existing five-language version caches and may produce read-only statistics. A new automatic reclamation round starts only when its specific switch is explicitly enabled.

Dependency preparation for all five languages, statistics, legacy cleanup, and recovery use one effective Worker policy and one version-cache capacity budget. Actual occupancy is the complete persistent filesystem tree, including manifests, ready markers, in-progress trash, and staging no longer covered by a live reservation. Manifest logical content bytes are not a substitute for total occupancy.

| Worker environment variable | Default | Range and meaning |
| --- | ---: | --- |
| `DLR_CACHE_GC_ENABLED` | `false` | Periodic reclamation; strict boolean |
| `DLR_CACHE_PRESSURE_GC_ENABLED` | `false` | Pressure reclamation for high watermark, low disk space, or an insufficient install reservation; strict boolean |
| `DLR_CACHE_SCAN_INTERVAL_SECONDS` | `300` | `10`–`86400` seconds |
| `DLR_CACHE_IDLE_TTL_SECONDS` | `2592000` | `60`–`31536000` seconds; minimum idle time for periodic candidates |
| `DLR_CACHE_MIN_IDLE_SECONDS` | `86400` | `0`–`IDLE_TTL_SECONDS`; pressure and manual cleanup also retain recent entries |
| `DLR_CACHE_MAX_BYTES` | `4294967296` | Positive integer; total version-cache budget |
| `DLR_CACHE_HIGH_WATERMARK_PERCENT` | `85` | High watermark; must satisfy `0 < LOW < HIGH < 100` |
| `DLR_CACHE_LOW_WATERMARK_PERCENT` | `70` | Target watermark for pressure reclamation |
| `DLR_CACHE_DISK_RESERVE_BYTES` | `134217728` | Non-negative and below `MAX_BYTES`; filesystem free space that must remain |
| `DLR_CACHE_MAX_DELETE_BYTES_PER_ROUND` | `268435456` | Positive and no greater than `MAX_BYTES`; an entry that cannot fit is retained |
| `DLR_CACHE_MAX_DELETE_ENTRIES_PER_ROUND` | `20` | `1`–`1000` |
| `DLR_CACHE_MAX_SCAN_ENTRIES_PER_ROUND` | `200` | `1`–`10000`; a stable cursor continues later rounds |
| `DLR_CACHE_MAX_SCAN_NODES_PER_ROUND` | `100000` | `1`–`1000000`; recursion within one entry consumes the same budget |
| `DLR_CACHE_MAX_SCAN_HASH_BYTES_PER_ROUND` | `268435456` | Positive; an incomplete hash leaves the entry unknown |
| `DLR_CACHE_MAX_SCAN_DEPTH` | `64` | `1`–`256`; directory symlinks are not followed |
| `DLR_CACHE_MAX_ROUND_SECONDS` | `10` | `1`–`60` seconds; no new object starts after the deadline |
| `DLR_CACHE_STAGING_TTL_SECONDS` | `86400` | `60`–`31536000` seconds; every other protection still applies after the TTL |
| `DLR_CACHE_OFFLINE_PROTECTION` | `true` | Source and offline protection is on by default; disabling it does not remove the valid-proof requirement |
| `DLR_CACHE_OFFLINE_MODE` | `false` | Explicit offline deployment; with protection enabled, only `verified_offline` or verified built-in material qualifies |
| `DLR_CACHE_SHARED_CACHE_MODE` | `report_only` | The only supported value in this release; shared download caches are reported and never reclaimed |

Workers validate this configuration strictly at startup. Invalid booleans, out-of-range values, a low watermark at or above the high watermark, or a disk reserve at or above the total budget reject startup. Values are never silently converted into unlimited cleanup or replaced with another policy. After changing the environment, restart the Worker in the normal change window and verify the effective policy in the Worker cache area; checking configuration text alone is insufficient.

## Before an entry may become a candidate

An idle version environment starts as `rebuildability=unknown` and cannot be reclaimed. It can become a candidate only when all of these facts hold:

- the current content identity and digest exactly match the confirmation;
- it is not pinned and has passed the applicable recent-use interval;
- a rebuilding confirmation is still valid, for at most 24 hours after confirmation;
- no Execution, Attempt, Slot, cleanup, Incident, recovery material, persistent use, or journal is responsible for it;
- Worker-root ownership, object identity, the Control guard, and local filesystem facts are all verified; and
- every condition is checked again immediately before the destructive rename.

Pin always wins. A content, identity, or digest change invalidates an older confirmation. A prior successful install, pinned dependency strings, or a URL that was once reachable is not sufficient evidence.

A rebuilding confirmation has one of two explicit meanings:

- `verified_offline`: the operator confirms that current, immutable, usable local material covers every dependency of the environment. It remains usable in explicit offline mode.
- `managed_online`: the operator confirms that a controlled, repeatable online source is currently usable. This is a time-limited operational assertion; the platform does not claim to probe or verify the external repository. Explicit offline mode protects it.

The confirmation binds the current identity and digest and includes a non-sensitive note, actor, confirmation time, and expiry. The platform may automatically issue `verified_offline` only for an environment with no external dependency whose built-in material identity has already been verified.

When dependency preparation reports a stable not-configured or unavailable result for the same source, older environments depending on that source are retained. The relation is a private stable identifier derived after credentials are removed; URLs, tokens, and log bodies are not stored. After checking the source or material, an operator must reconfirm the current identity and digest to clear this protection.

## Classification and capacity policy

The Worker cache area reports actual occupancy by class:

- **Version environments:** verifiable Adapter version caches. Only entries that pass every protection are included in estimated reclaimable bytes.
- **Shared download caches:** shared uv, npm, Maven, and Go roots report `shared_cache_not_supported`. Cleaning one Adapter never deletes them.
- **Failed staging:** eligible only when its controlled name and ownership are verified, its reservation is inactive, its TTL has passed, and no process, use, journal, or recovery operation remains.
- **In-progress trash:** belongs to an existing durable operation, remains part of actual occupancy, and resumes under its original operation and generation.
- **Unknown directories or legacy layouts:** report `cache_ownership_unknown` and remain in place. Names are not used to guess ownership.

Periodic reclamation selects only safe entries older than `IDLE_TTL_SECONDS`. Pressure and manual cleanup also enforce `MIN_IDLE_SECONDS`. Selection is oldest last-use first; equal timestamps prefer larger entries, then stable key order. Reclamation triggered by an insufficient installation reservation stops when that reservation can be satisfied. Background pressure reclamation stops when occupancy reaches the low watermark and the disk safety reserve has been restored. Existing round budgets and protection checks still apply.

Each failed install reservation may run at most one bounded pressure round and then retry the reservation once. No candidate, insufficient recovered capacity, and an entry larger than the remaining round budget produce stable reasons; they never trigger an unbounded loop or forced deletion. Periodic, pressure, and manual work share one per-Worker round lock. Version-key locks are acquired without waiting, preventing a running prepare and a governance round from deadlocking each other.

## Management contract

The final management surface belongs in the Worker's cache area. It reports sample time, completeness, effective policy, occupancy by class, estimated reclaimable bytes, and retention reasons. An offline, stale, or incomplete paginated snapshot is observation only and cannot authorize deletion.

Administrator operations follow these rules:

- Preview describes one sample; execution rechecks every Control and Worker fact.
- Only one management operation may be active for a Worker. Repeating the same idempotent request returns the original operation instead of adding cleanup.
- One operation may name at most 200 keys. A selected-key operation cannot clean unselected entries.
- Protect binds the most recently observed identity and digest. A conflict is rejected instead of overwriting newer content.
- Operation and audit records contain only non-sensitive IDs, stable reasons, estimated and actual freed bytes, operation/generation, and timestamps.
- Audit queries are limited to 100 rows per page. Terminal audit defaults to 90 days and at most 1,000 rows per Worker. An unfinished operation or guard, or audit still referenced by cleanup, is never removed by retention.

A failed Adapter cleanup is retried through its original `cleanup_id`, not a second cleanup channel. For example, an operator retry of `failed/3` first returns it to `pending/3`; the next real claim becomes `running/4`. If that attempt fails, a new operator retry is required before a fifth real claim. Each operator retry grants one logical attempt. Budget continuation and Worker restart remain within that attempt, old claim results cannot complete a newer claim, and the attempts counter is never reset.

After a managed cleanup fails, its original failed audit remains available. A later explicit retry has its own operation record and includes completed children from the original cleanup without counting freed bytes twice. A new retry is rejected once the target has been recovered; repeating the same idempotent request still returns its original operation.

## Disconnection, failure, and recovery

When Control, journal, use, root owner, identity, digest, source, or recovery responsibility cannot be verified, the entry is retained. Unknown journals, symlinks, corrupt sidecars, open Incidents, deferred cleanup, and active Attempts cannot be bypassed by a manual request or configuration switch.

Deletion uses a durable guard, operation/generation, atomic rename into private trash, and bounded continuation. An interruption after rename must continue or safely stop the original operation. Trash cannot become a new candidate, and an old terminal receipt cannot release a newer generation. After three real consecutive failures, automatic hot retry stops and waits for an explicit administrator retry, which still performs every protection check.

Disabling `GC_ENABLED` or `PRESSURE_GC_ENABLED` stops only new automatic rounds of that kind. It does not revoke a guard already acquired or a transaction that has already renamed its source. Recovery must still converge that durable operation, or responsibility for an already-performed filesystem action would be lost.

Do not “repair” capacity by clearing Worker volumes, deleting lifecycle or journal data, moving cache directories by hand, fabricating receipts, or bypassing guards. Do not downgrade to a version that cannot understand active cache responsibility or an unfinished governance operation. Upgrade and recovery preserve Worker runtime and journal volumes and first converge the current operation and generation safely forward.

## Before enabling automatic reclamation

At minimum, verify that:

1. The deployed release contains the complete guard, use/journal, recovery, and policy implementation, rather than configuration fields alone.
2. The effective policy shown in the Worker cache area matches the plan and the default disabled state has not changed accidentally.
3. Every proposed entry has a current, time-limited confirmation covering all dependencies; shared and unknown occupancy is excluded from reclaimable bytes.
4. A dedicated test environment covers low-capacity installation, concurrent use, disconnection, restart, and partial-trash recovery. A build or unit test does not replace runtime acceptance.
5. Begin with conservative budgets and thresholds, observe actual occupancy, and then adjust. Reducing a budget is not a force-delete mechanism.
