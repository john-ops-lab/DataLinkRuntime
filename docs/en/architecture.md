# DLR（DataLinkRuntime）Overall Architecture

> Implementation baseline for this change: `v0.3.0` / `d28daabfe9e70a5d5db23fb62613c5c39222764b` (including Issue #130).
> This document describes the currently implemented architecture. Historical stage contracts live in `docs/specs/README.md`, historical Specs and the Alembic migrations. New work is governed by the currently authorized GitHub Issue.

## 1. Component Overview

```text
┌──────────────┐  HTTP/JSON + SSE  ┌─────────────────────────────┐
│ web (React)  │ ─────────────────► │ control (FastAPI)           │
└──────────────┘                    │ API / Schedule / Webhook    │
                                    └──────┬──────────────┬───────┘
                              transaction │              │ bounded publish
                                    ┌──────▼──────┐ ┌────▼──────────────┐
                                    │ PostgreSQL  │ │ RabbitMQ 4.3      │
                                    │ authority   │ │ Quorum Queue      │
                                    └──────▲──────┘ └────┬──────────────┘
                                           │ Claim/renew │ dispatch
                                    ┌──────┴─────────────▼──────────────┐
                                    │ worker v3 (Python/Node.js/Java)  │
                                    └───────────────────────────────────┘
```

| Component | Responsibility | Runs user code |
|-----------|----------------|----------------|
| web | Adapter Catalog, Template Gallery, Workbench, Monaco, run control, live logs and history | No |
| control | APIs, static template catalog and instantiation, transactional gates, scheduling, Webhook routing, log SSE, thin AI Provider adapter | No |
| postgres | Adapter, Execution, Admission, Outbox, Attempt, Slot, Lease, and audit authority | No |
| rabbitmq | Bounded dispatch, delayed retry, and Infrastructure DLQ; one node is not HA | No |
| worker | Consumes dispatch, Claims through Control, prepares dependencies, runs Sandboxes, reports incrementally | Yes |

Worker only makes outbound connections to RabbitMQ and Control; Control never
connects back to Worker. Every trigger mode executes user code on Worker, and Worker
never accesses PostgreSQL directly.

## 2. Authentication and Sensitive Data

| Channel | Credential |
|---------|------------|
| Admin Web/API → Control | `DLR_ADMIN_TOKEN` |
| Worker → Control | `DLR_WORKER_TOKEN` |
| External systems → Webhook | the Adapter's bound token Credential |

Credentials are encrypted with a Fernet key derived from the deployment-level
`DLR_MASTER_KEY`. The browser only receives Credential metadata; Control decrypts
only at the necessary moments for claim, Webhook validation or AI Provider requests.
Plaintext is never persisted and never enters ordinary logs.

## 3. Current Domain Model

### 3.1 Adapter

Key fields:

| Field | Current semantics |
|-------|-------------------|
| `id / name / description` | Basic info; name / description stay editable while running |
| `language` | `python / javascript / java`, immutable after creation |
| `adapter_type` | `task / webhook` |
| `run_mode` | `manual / schedule` for Task; unused by Webhook |
| `latest_version_id` | the latest saved immutable Revision |
| `runtime_worker_id` | the current run node |
| `template_scenario_slug / template_version` | optional, paired, read-only template provenance; both are null for ordinary Adapters |
| `archived_at` | internal soft-delete marker; the current Web UI shows active Adapters only |

API responses carry `runtime_locked` and `running_execution_id` so the Web UI can
directly display the authoritative run lock and current Execution, never replacing
server facts with browser time or local inference.

### 3.2 AdapterVersion / Revision

Every save creates one immutable record — `code / requirements / runtime_config /
seq / created_at` — and updates `latest_version_id`. The UI only offers “Save”;
since M5.5.9 the Revision sequence number is no longer shown to ordinary users (the
header was consolidated; the underlying audit fact is unchanged).

Run entries always bind to the `latest_version_id` captured at Execution creation,
so a later save never changes an already running Execution.

### 3.3 Execution

| Field | Semantics |
|-------|-----------|
| `adapter_id / version_id` | the fixed Adapter and Revision |
| `worker_id / target_worker_id` | the claiming node / the requested run node |
| `trigger` | `manual / schedule / webhook` |
| `scheduled_for` | the Schedule plan point; NULL for other triggers |
| `dispatch_backend / dispatch_generation` | the `legacy` or `rabbitmq` responsibility boundary and current message generation |
| `status` | legacy `pending/running/...`, or RabbitMQ `queued/running/retry_wait/dead_letter/...` |
| `input / output / stdout / stderr` | input, result and logs |
| `cancel_requested` | the cancellation request while running |

During compatibility, a partial unique index permits at most one legacy
`pending/running` Execution per Adapter. The RabbitMQ backend may keep multiple valid
`queued/retry_wait` Executions, while `adapter_execution_slots(adapter_id,
slot_no=0)` plus the active-Attempt unique constraint permits at most one physical
execution per Adapter. Manual, Schedule, and Webhook share Admission, Outbox,
Attempt, and Slot contracts.

### 3.4 Worker

Workers report stored status, heartbeat and capability. Control derives
effective-online from database time:

```text
status == online
AND clock_timestamp() - last_heartbeat <= heartbeat_timeout
```

The timeout must be positive and strictly larger than the Worker heartbeat interval.
RabbitMQ ingress requires the fixed Worker to exist and have compatible language,
protocol v3, and isolation capabilities. A temporarily offline target may still
receive bounded `queued` responsibility and waits for that same Worker; it is never
silently rerouted.

### 3.5 Credential and Bindings

- `credentials`: name, type, Fernet ciphertext and timestamps;
- `adapter_credential_bindings`: `env_key → credential.field`;
- `package_sources`: Python / npm / Maven dependency sources, bindable to a
  Credential;
- Webhook only allows binding a `token`-type Credential.

### 3.6 Template Catalog, Variants, and Instantiation

Templates are not hidden database Adapters. The Control wheel/image carries read-only
package resources: 5 Themes, 17 Scenarios, 51 Python/JavaScript/Java Variants, JSON
Schemas, provenance records, and per-language maturity Receipts. Startup validation
fails closed on stable slugs, versions, languages, Logo keys, cross-references, and
source SHA-256. Scenario lists and details read metadata only; just the selected source
is read and bounded-cached by the single-language endpoint:

```text
GET  /api/templates/themes
GET  /api/templates/scenarios
GET  /api/templates/scenarios/{scenario_slug}
GET  /api/templates/scenarios/{scenario_slug}/variants/{language}
POST /api/templates/scenarios/{scenario_slug}/variants/{language}/instantiate
```

The instantiate POST uses the reviewed `expected_template_version` to prevent rolling
deployment drift. One transaction writes the Adapter, Slot 0, minimum disabled type
configuration, Revision 1, and latest pointer. It does not use ordinary Clone and does
not create a Credential/Binding, installed Dependency, Managed File/Artifact/Lease,
Worker, Schedule, extra ACL, Execution, Admission, Outbox, Attempt, or history. The new
Adapter starts stopped and is independent of future template changes. On success the
Web refreshes the Adapter list, loads Revision 1, and opens that Adapter in the editor.

`DLR_MANAGED_FILES_ENABLED=false` disables the corresponding runtime capability only;
it does not affect discovering, reading one language, or copying any template,
including CSV and Excel, and instantiation never fabricates a file binding.

Maturity binds `scenario_slug + version + language + source_sha256`. A
`syntax-verified` Receipt must show exact dependency resolution and that language's
syntax/compile check. Only a fixed fixture that directly executes the same shipped
source can establish `fixture-verified`, and only a controlled read-only run against a
real external service can establish `live-verified`. Static catalog checks, a sibling
language, or a helper implementation cannot substitute for those results.
`reference-generated` means no Receipt both matches the current source hash and
satisfies every gate for the next level; narrow smoke or security-canary execution is
not maturity-promotion evidence.

## 4. Task Execution

### 4.1 Manual

```text
POST /api/adapters/{id}/executions
→ lock the Adapter
→ snapshot the latest Revision, input, Credential references and target Worker
→ atomically create Execution + Admission + Outbox in PostgreSQL
→ Relay publishes a RabbitMQ dispatch
→ Worker v3 Claim / journal / ACK / Sandbox / report
```

Cancellation reuses `POST /api/executions/{id}/cancel`. Legacy pending and RabbitMQ
queued/retry_wait work can go straight to cancelled; a running Execution sets the
cancel request and Worker terminates the Sandbox process under the current Attempt
fence before reporting terminal state.

### 4.2 Schedule

`adapter_schedules` is the per-Task-Adapter singleton configuration: enabled, cron,
timezone, next_run_at, one of `coalesce_latest / queue_every_occurrence /
skip_while_busy`, and bounded catch-up settings. Input comes from the unified saved
InputConfig.

Control uses PostgreSQL as the only scheduling state source and polls due rows in
short transactions; multiple Control instances divide work with
`FOR UPDATE SKIP LOCKED`. Every tick evaluates due-ness with `clock_timestamp()`,
evaluates the Cron in the configured timezone, and stores the result in UTC.

Enabling a Schedule locks its run configuration. Every crossed plan point has an
`enqueued/coalesced/skipped/expired` audit outcome. The saved policy decides whether
to queue every occurrence, coalesce to the latest point, or skip while busy.
Admission failures retain or explicitly consume responsibility according to policy;
processing does not hot-loop or jump past the first unowned point.

## 5. Webhook

`adapter_webhooks` is the per-Webhook-Adapter singleton configuration: enabled,
public_id, token credential and timestamps.

In the stopped state multiple Adapters may use the same `public_id`; the PostgreSQL
partial unique index constrains path uniqueness to `enabled=true` rows only. When
starting reception the service layer returns a stable conflict code first, with the
database index as the final concurrency defense.

External entry:

```text
POST /api/hooks/{public_id}
Authorization: Bearer <token>
Content-Type: application/json
```

Validation covers body size, enabled route, Bearer Token, JSON contract, fixed run
node, and Admission in order. On success it atomically snapshots the latest Revision,
Credential references, and complete JSON body as immutable JSON input, creates the
`trigger=webhook` Execution plus Outbox, and returns `202` immediately. Control does
not wait for Worker completion.

Every successful reception is one call record. Retention governs terminal history in
bounded batches using deployment-configured days and per-Adapter limits per trigger;
it never deletes active Executions or rows still needed for recovery.

## 6. Clone and Deletion

Clone copies current code, dependencies, run parameters, Credential references,
trigger configuration and the run node inside one transaction. The new Adapter starts
from its own first Revision, has no Executions, and its Schedule / Webhook are both
disabled.

A running Webhook A can be cloned into a same-path stopped B; only after A stops can
B start receiving, keeping the external URL unchanged.

Deletion requires `runtime_locked=false` and no active Execution. The current
implementation writes a soft-delete marker; the active Catalog never returns deleted
Adapters, and the Web UI offers no restore entry.

## 7. Workbench and Live Logs

The Workbench is fixed to three tabs per type:

```text
Task:    Edit / Run Settings / Execution History
Webhook: Edit / Run Settings / Call History
```

The Header shows Adapter name, type, language, run status, run node, dirty state,
Save and type-specific actions. Disabled controls expose a stable reason through a
focusable wrapper; Monaco and all run-configuration controls follow
`runtime_locked`.

There is exactly one live watcher per Workbench:

1. create or discover the active Execution;
2. open SSE via `GET /api/executions/{id}/events`;
3. incrementally merge stdout / stderr;
4. on abnormal disconnect, poll the authoritative Execution API with a bounded
   backoff;
5. refresh the Adapter run status after the terminal state.

The log workspace sits at the bottom of the page, supports fullscreen and restore.
Webhook shows a waiting state until an Execution exists. History details open
independently on user selection and are never replaced by background Schedule
creations.

## 8. Runtime

All three languages execute in a fresh sub-process and exchange JSON input/output
through files:

- Python: version-scoped `.venv`;
- JavaScript: version-scoped `node_modules` and an ESM harness;
- Java: version-scoped Maven deps, classes and JVM.

Dependency preparation is uniformly offline-first. stdout / stderr are uploaded
incrementally and redacted; over-limit content is truncated per configuration.
Oversized input is rejected outright without creating an Execution; oversized output
keeps only the size, truncation flag and preview, never storing corrupted JSON.

The v3 order is `dispatch → durable Control Claim → private journal → ACK →
Sandbox`. ACK never waits for business completion; a post-ACK crash is recovered by
database Attempt Lease/Fencing, Recovery, and a new generation. For every Attempt,
the Sandbox applies hard CPU, memory, PID, tmpfs, and output bounds inside an exact
delegated Linux cgroup v2 subtree. A non-Linux or incomplete preflight fails closed.

### 8.1 External-call Boundary for Template Recipes

After copying, a Recipe is an ordinary user Adapter and remains subject to the Worker,
Revision, Credential, and Execution contracts above. Non-secret endpoints and limits
belong in Revision runtime config or immutable Execution Input; secrets are injected
only through Credential Binding. URL scheme, same-origin redirect, timeout, and size
checks are per-Recipe misuse guards, not a platform-level SSRF, DNS-rebinding, or
egress-isolation boundary. Trusted administrators must restrict Worker egress with
deployment firewalls, DNS/proxy policy, and destination allowlists.

The seven cloud/CMDB Recipes use `preview` only to read sources; the normalized
`dlr-asset-snapshot/v1` and final Adapter Output are bounded. `sync` writes to an
administrator-configured external `dlr-cmdb-upsert/v1` target, not a DLR Control API.
Stable `scan_id` and `source_scope` values in immutable Input make a new Attempt of the
same Execution reuse the same begin/batch/finish idempotency identities. Any source or
batch failure must return `partial=true` and skip finish so an incomplete scan cannot
trigger target-side stale cleanup. The Alibaba Cloud SDK `callApi` transport used by
three Alibaba Recipes does not yet have a proven source-response byte bound, so bounded
output does not imply bounded raw HTTP transport.

## 9. AI Assistant

The browser explicitly submits the current Working Copy, user instructions and a
bounded recent conversation. Control adds server-side language, base Revision
metadata, the Runtime Contract and Secret env-key names. The Provider's final answer
must pass Candidate Schema validation.

For one-request attachments, XLSX opens only bounded ZIP/XML members and XLS uses the
pinned `xlrd` in-memory BIFF entry point. Both reuse file, inflation, character, and
parse-time budgets and never execute formulas, macros, or external relationships.
Current `managed_files` contributes only an ordinal-ordered narrow database projection
and the three-language Context file API; it never reads ArtifactStore, creates a Lease,
or exposes Artifact IDs, storage keys, paths, Tokens, or file contents.

Candidate Apply only changes the browser Working Copy; a stale Candidate needs
explicit re-confirmation. Credential true values, platform Tokens, Provider
reasoning, Prompts and raw Responses never enter ordinary logs or persistence.

## 10. Deployment and Configuration

Docker Compose runs six services: `web / account-web / control / postgres / rabbitmq
/ worker`. Defaults keep ordinary RabbitMQ ingress off, legacy Claim on, and all
three Cutover attestations off. Key configuration includes:

- `DLR_ADMIN_TOKEN`, `DLR_WORKER_TOKEN`, `DLR_MASTER_KEY`;
- `DLR_WORKER_HEARTBEAT_SECONDS` and `DLR_WORKER_HEARTBEAT_TIMEOUT_SECONDS`;
- `DLR_SCHEDULE_POLL_SECONDS`;
- finite RabbitMQ Queue/Outbox/Admission/Attempt/Dead Letter bounds;
- `DLR_MIN_WORKER_PROTOCOL_VERSION`, `DLR_LEGACY_EXECUTION_CLAIM_ENABLED`, and the
  three `DLR_CUTOVER_*_GATE_PASSED` attestations;
- Execution large-field, log, timeout and Worker concurrency limits.

The AI Provider is an external deployment dependency and never enters the formal
Compose topology. compose-smoke starts a local fake Provider in an isolated network
only.

## 11. System Language and Internationalization

- The `system_settings` singleton row stores the deployment-level system language
  (default `zh-CN`), allowing only `zh-CN / en`; the public read-only endpoint
  `GET /api/locale` returns only the current language (available before admin
  login), and administrators change and persist it via `PUT /api/locale`;
- Unauthenticated administrator and account login pages use a separate browser-local
  `dlr-login-locale` preference, defaulting to `zh-CN` on first visit. It never enters
  an account or the database. Authentication and forced password change restore the
  server system locale; a failed locale request falls back to a valid system cache or
  safe default without blocking authentication;
- `executions.locale` captures the system language at Execution creation and stays
  fixed; switching the system language while running does not change that
  Execution's later platform messages;
- Platform messages generated by Control / Worker are rendered through built-in
  zh-CN / en templates; user-code stdout / stderr, Tracebacks and raw third-party
  tool output never enter the translation layer;
- The Web uses i18next with bundled resources (six namespaces:
  `common / adapter / runtime / settings / ai / template`); the zh-CN / en key sets are
  identical, and a missing key falls back to a safe placeholder instead of showing
  the raw key;
- A language switch never modifies any Adapter code / Revision, Credential true
  values or existing Execution logs; the Secret redaction contract for errors and
  logs is unchanged.

## 12. Verification Gates

- Backend: Ruff, format check, Mypy, full pytest (including the README / key docs
  bilingual pairing, mutual-link and relative-link resolution checks, and the
  zh-CN / en translation-resource key and placeholder parity checks);
- Templates: exact 5/17/51 inventory, Schema/provenance/license/source-hash checks,
  wheel/image package resources, requirements parsers, and per-language Receipt gates;
  syntax, fixture, and live evidence are recorded as distinct layers;
- Web: ESLint, TypeScript, Vitest (including locale namespace / leaf-key /
  interpolation-placeholder parity checks), production build;
- Database: fresh Alembic install and upgrade from the current main schema;
- Integration: isolated Compose smoke with real three-language Task, Schedule,
  Webhook, Clone URL handover and run-lock runs;
- Reliable Runtime: real broker outage/restart, Confirm ambiguity, Worker/Control
  crash, Slot pressure, DLQ/Replay, post-Cutover invariants, and bounded resources;
- Sandbox: only real target Linux cgroup v2 + private cgroup namespace + exact
  delegated-subtree Compose evidence counts; host cgroup namespace is `NO_COUNT`, and
  macOS or static configuration does not count either;
- UI: real-browser verification of bottom/fullscreen logs, run lock, Clone/Delete
  and the Task / Webhook main paths.

## 13. Explicit Boundaries

Currently absent: synchronous Webhook, URL takeover, resident process model, RBAC,
generic plugin system, workflow orchestration, a separate log system, AI
auto-execution loop, user-level language preference, machine translation of user
content, a third language, or a multi-node RabbitMQ HA cluster. Bounded Retry/Recovery
for one Execution does not expand into workflow retry.

## 14. Reliable Runtime Cutover and Rollback

### 14.1 Non-interchangeable Cutover

The order is fixed: prove backup/restore → inventory/preflight → drain legacy running
and migrate pending → Worker v3 plus Sandbox → ordinary RabbitMQ ingress → Slot
pressure test → minimum protocol 3 → retire the legacy active index → close legacy
Claim only after legacy active reaches zero. All mutation endpoints require admin
authentication; inventory, preflight, and post-Cutover invariants are read-only.

### 14.2 Compatible Recovery Boundary

Before Cutover, new ingress can be closed while legacy continues. Once the old index
is retired or RabbitMQ rows exist, rollback must preserve the additive schema and use
the current compatible Control to drain/repair Outbox, Attempt, Slot, and Incident
responsibility. Never start an old binary against new rows, run a destructive schema
downgrade, or simply disable ingress so new requests fall toward an already-closed
legacy Claim. See [Reliable Runtime migration notes](issue130-reliable-runtime-migrations.md)
for the operational sequence and API.
