# DLR（DataLinkRuntime）Overall Architecture

> Current baseline: `v0.1.0` (M5.11 is complete and has passed final user acceptance).
> This document describes the currently implemented architecture. Historical stage contracts live in `docs/specs/README.md`, historical Specs and the Alembic migrations. New work is governed by the currently authorized GitHub Issue.

## 1. Component Overview

```text
┌──────────────┐  HTTP/JSON + SSE  ┌─────────────────────────────┐
│ web (React)  │ ─────────────────► │ control (FastAPI)           │
└──────────────┘                    │ Adapter / Execution / AI API│
                                    │ Schedule poller / Webhook   │
                                    │ PostgreSQL                  │
                                    └──────────────┬──────────────┘
                                                   │ Worker long polls actively
                                    ┌──────────────▼──────────────┐
                                    │ worker (multi-runtime agent)│
                                    │ Python / Node.js / Java     │
                                    └─────────────────────────────┘
```

| Component | Responsibility | Runs user code |
|-----------|----------------|----------------|
| web | Catalog, Workbench, Monaco, run control, live logs and history | No |
| control | API, transactional gates, scheduling, Webhook routing, log SSE, thin AI Provider adapter | No |
| postgres | Adapter, Revision, Execution, Worker, Trigger and Credential | No |
| worker | Claims tasks, prepares dependencies, executes in separate sub-processes, reports incrementally | Yes |

The Worker only connects to Control actively; Control never connects back to the
Worker. Every trigger mode executes user code on a Worker.

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
| `status` | `pending / running / succeeded / failed / timeout / cancelled` |
| `input / output / stdout / stderr` | input, result and logs |
| `cancel_requested` | the cancellation request while running |

A partial unique database index guarantees at most one `pending / running` Execution
per Adapter at a time; Manual, Schedule and Webhook share the same constraint. Every
service entry uses the unified Adapter row-lock order, with the database constraint
as the final concurrency defense.

### 3.4 Worker

Workers report stored status, heartbeat and capability. Control derives
effective-online from database time:

```text
status == online
AND clock_timestamp() - last_heartbeat <= heartbeat_timeout
```

The timeout must be positive and strictly larger than the Worker heartbeat interval.
Every run entry requires the node to be effective-online and its capability to cover
the Adapter language.

### 3.5 Credential and Bindings

- `credentials`: name, type, Fernet ciphertext and timestamps;
- `adapter_credential_bindings`: `env_key → credential.field`;
- `package_sources`: Python / npm / Maven dependency sources, bindable to a
  Credential;
- Webhook only allows binding a `token`-type Credential.

## 4. Task Execution

### 4.1 Manual

```text
POST /api/adapters/{id}/executions
→ lock the Adapter
→ validate the latest Revision, run node and the unified run lock
→ create a trigger=manual Execution
→ Worker claim / run / report
```

Cancellation reuses `POST /api/executions/{id}/cancel`. A pending Execution can go
straight to cancelled; a running one sets the cancel request and the Worker
terminates the process group and reports the terminal state.

### 4.2 Schedule

`adapter_schedules` is the per-Task-Adapter singleton configuration: enabled, cron,
timezone, input, next_run_at.

Control uses PostgreSQL as the only scheduling state source and polls due rows in
short transactions; multiple Control instances divide work with
`FOR UPDATE SKIP LOCKED`. Every tick evaluates due-ness with `clock_timestamp()`,
evaluates the Cron in the configured timezone, and stores the result in UTC.

Enabling a Schedule locks the run configuration. At the due point an Execution with
`trigger=schedule` is created from the latest Revision, the current run node and the
configured Input. Offline Workers or busy Adapters are not queued; at most the most
recent missed plan point is caught up once conditions recover. Disabling or changing
the configuration re-bases the cursor to the next future point.

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

Validation covers body size, enabled route, Bearer Token, JSON contract, run node and
the unified run lock in order. On success an Execution with `trigger=webhook` is
created from the latest Revision, the current run node and the full JSON body, and
`202` is returned immediately; Control does not wait for the Worker to finish.

Every successful reception is one call record. Retention keeps the most recent 100
terminal Webhook Executions per Adapter, never deletes active Executions, and never
touches Task / Schedule history.

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

## 9. AI Assistant

The browser explicitly submits the current Working Copy, user instructions and a
bounded recent conversation. Control adds server-side language, base Revision
metadata, the Runtime Contract and Secret env-key names. The Provider's final answer
must pass Candidate Schema validation.

Candidate Apply only changes the browser Working Copy; a stale Candidate needs
explicit re-confirmation. Credential true values, platform Tokens, Provider
reasoning, Prompts and raw Responses never enter ordinary logs or persistence.

## 10. Deployment and Configuration

Docker Compose runs four services: `web / control / postgres / worker`. Key
configuration includes:

- `DLR_ADMIN_TOKEN`, `DLR_WORKER_TOKEN`, `DLR_MASTER_KEY`;
- `DLR_WORKER_HEARTBEAT_SECONDS` and `DLR_WORKER_HEARTBEAT_TIMEOUT_SECONDS`;
- `DLR_SCHEDULE_POLL_SECONDS`;
- Execution large-field, log, timeout and Worker concurrency limits.

The AI Provider is an external deployment dependency and never enters the formal
Compose topology. compose-smoke starts a local fake Provider in an isolated network
only.

## 11. System Language and Internationalization

- The `system_settings` singleton row stores the deployment-level system language
  (default `zh-CN`), allowing only `zh-CN / en`; the public read-only endpoint
  `GET /api/locale` returns only the current language (available before admin
  login), and administrators change and persist it via `PUT /api/locale`;
- `executions.locale` captures the system language at Execution creation and stays
  fixed; switching the system language while running does not change that
  Execution's later platform messages;
- Platform messages generated by Control / Worker are rendered through built-in
  zh-CN / en templates; user-code stdout / stderr, Tracebacks and raw third-party
  tool output never enter the translation layer;
- The Web uses i18next with bundled resources (five namespaces:
  `common / adapter / runtime / settings / ai`); the zh-CN / en key sets are
  identical, and a missing key falls back to a safe placeholder instead of showing
  the raw key;
- A language switch never modifies any Adapter code / Revision, Credential true
  values or existing Execution logs; the Secret redaction contract for errors and
  logs is unchanged.

## 12. Verification Gates

- Backend: Ruff, format check, Mypy, full pytest (including the README / key docs
  bilingual pairing, mutual-link and relative-link resolution checks, and the
  zh-CN / en translation-resource key and placeholder parity checks);
- Web: ESLint, TypeScript, Vitest (including locale namespace / leaf-key /
  interpolation-placeholder parity checks), production build;
- Database: fresh Alembic install and upgrade from the current main schema;
- Integration: isolated Compose smoke with real three-language Task, Schedule,
  Webhook, Clone URL handover and run-lock runs;
- UI: real-browser verification of bottom/fullscreen logs, run lock, Clone/Delete
  and the Task / Webhook main paths.

## 13. Explicit Boundaries

Currently absent: MQ, request queue, automatic retry, synchronous Webhook, URL
takeover, resident process model, RBAC, generic plugin system, workflow
orchestration, a separate log system, AI auto-execution loop, user-level language
preference, machine translation of user content, or a third language.
