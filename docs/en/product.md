# DLR（DataLinkRuntime）Product Definition

> The current source baseline includes the Issue #130 Reliable Runtime. Release,
> Hosted CI, independent Review, and user acceptance remain separate states.
> This document describes the currently implemented product model. Historical decisions live in `docs/specs/README.md`, historical Specs and the database migration history. New work is governed by the currently authorized GitHub Issue.

## 1. Product Positioning

DLR is a lightweight data adapter runtime platform for data collection, receiving,
parsing, transformation and output for CMDB and other systems.

Core goals:

- One server and Docker Compose are enough to deploy the full platform;
- Adapter creation, editing, saving, running, stopping, Clone upgrade and
  troubleshooting all happen in the browser;
- Python, JavaScript and Java share one consistent input / output / log experience;
- The AI Assistant only produces Candidate changes; saving and running are always
  explicit administrator actions;
- It must not grow into a workflow engine, a low-code platform or a generic plugin
  platform.

## 2. Core Objects

| Object | Current definition |
|--------|--------------------|
| Adapter | An independent data processing unit of type Task or Webhook |
| Revision | The immutable code, dependency and run-parameter snapshot produced by each save; an internal audit fact |
| Execution | One logical run, fixing input, Revision, target Worker, backend, generation, state and result |
| Attempt / Slot | One physical RabbitMQ execution attempt and the per-Adapter `Slot 0` concurrency authority |
| Worker | The node that runs user code, selected by language, protocol and isolation capability |
| Credential | An encrypted credential; the browser never receives its true value |

Users only perform “Save”. The system creates an immutable Revision in the background
and pins later runs to the latest saved content.

### 2.1 Template Gallery and Recipe Instantiation

Template Gallery is a top-level destination alongside Adapters. Its initial static,
release-versioned catalog contains exactly 5 Themes, 17 Scenarios, and one Python,
JavaScript, and Java implementation per scenario: 51 Variants in total. Users can
search by theme, keyword, vendor, Adapter type, protocol, language, and maturity. A
detail view loads code, contracts, dependency guidance, and provenance only for the
selected language Variant.

After the user selects a language, enters a name, and confirms, one transaction creates
an independent Adapter, Slot 0, the minimum disabled type configuration, and Revision 1.
The Web then opens that Adapter in the editor. This is not ordinary Clone behavior: the
new Adapter starts stopped and has no Worker, Credential Binding, installed Dependency,
Schedule, Managed File, Execution, or history. Later template releases never overwrite
the copied code. Before running, the user must review or edit it, choose a compatible
Worker, install exact dependencies, and configure input.

Non-secret Recipe settings belong in `context.config` or Execution Input. Passwords,
Tokens, private keys, and similar values are supplied only through Credential Binding
and `context.secrets`. External endpoints are administrator-reviewed runtime settings;
copying a template does not make them trusted, and authentication query values must not
be embedded in them. Recipe HTTPS, same-origin redirect, timeout, and limit checks are
not a platform-level SSRF or egress-isolation boundary. Production deployments still
need network policy around Worker destinations.

Seven cloud/CMDB Scenarios provide a read-only `preview` whose normalized result and
Adapter Output are bounded by page, record, byte, and total-request-time limits. Optional
`sync` targets the external `dlr-cmdb-upsert/v1` contract and obtains stable `scan_id`
and `source_scope` values from immutable Execution Input. Every Attempt of one logical
Execution must reuse both values, and any source or batch failure must skip finish. The
Alibaba Cloud SDK `callApi` transport used by three Alibaba Scenarios does not yet have
a proven source-response byte bound, so its raw HTTP response is outside this bounded-
output claim. This is only the identity fragment, not a complete Variant input contract:

```json
{
  "mode": "sync",
  "scan_id": "123e4567-e89b-42d3-a456-426614174000",
  "source_scope": "alicloud:EXAMPLE_ACCOUNT:example-region-1"
}
```

The UUID above is an anonymous, copyable example. Use a new value for a new business
scan and reuse the original value for every retry of that same scan.

`DLR_MANAGED_FILES_ENABLED=false` does not affect browsing, source viewing, or copying
the 5/17/51 catalog, including CSV and Excel. Copying does not create a file, Artifact,
Lease, or binding. At runtime, provide direct content or a file only as allowed by the
selected Variant contract and deployment capability.

Maturity is independent for each `scenario + version + language + source_sha256`:
`reference-generated / syntax-verified / fixture-verified / live-verified`. A matching
Receipt constrains every label. `reference-generated` means there is no Receipt that
both matches the current source hash and satisfies every gate for the next level;
narrow smoke or security-canary execution may exist without being promotion evidence.
Syntax or compilation proves at most `syntax-verified`; it is never complete fixture or
live-service evidence. See [Template Recipe usage and security boundaries](../templates/recipe-usage-security.en.md),
the [CMDB Upsert v1 contract (Simplified Chinese)](../templates/cmdb-upsert-v1.md), and
[maturity Receipts (Simplified Chinese)](../templates/maturity-receipts.md) for the
detailed contracts.

## 3. Adapter Types

### 3.1 Task Adapter

Task supports two run modes:

- Manual run: `Run Once` / `Stop Running`;
- Scheduled run: configure Cron, Timezone and Input, use `Enable Schedule` /
  `Disable Schedule`, and optionally `Run Once Now`.

The page information architecture is fixed as:

```text
Edit
Run Settings
Execution History
```

### 3.2 Webhook Adapter

Webhook is served by Control through one unified entry:

```text
POST /api/hooks/{public_id}
Authorization: Bearer <token>
```

Users configure a readable path, a Token Credential and the run node, and use
`Start Receiving` / `Stop Receiving`. After validation the request asynchronously
creates an Execution and returns `202 + execution_id`. The RabbitMQ backend may keep
multiple immutable `queued/retry_wait` Executions, while one Adapter still has only
one active Attempt. The legacy backend keeps its original single-active gate during
the compatibility window.

The page information architecture is fixed as:

```text
Edit
Run Settings
Call History
```

Webhook, Task, and Schedule terminal Executions are cleaned in retryable batches
using deployment-configured age and per-Adapter count limits. `pending` and
`running` rows are never removed by retention. See
`docs/deployment/platform-logs.md` for defaults and platform-log rotation.

## 4. Saving and the Run Node

- The page only shows “Save”;
- The first save must determine an effective-online, language-compatible run node;
- A single compatible node is selected automatically; with multiple nodes the user
  chooses explicitly;
- Later saves keep using the current run node, which can be viewed or changed in Run
  Settings;
- Every run entry uses the latest saved content and the Adapter's current run node.

## 5. Unified Run Lock

The legacy backend permits at most one `pending/running` Execution per Adapter. The
RabbitMQ backend permits bounded queueing, while database `Slot 0` binds at most one
active Attempt. Task manual runs, Schedule, and Webhook share the same Admission,
snapshot, and Slot rules.

When a Schedule/Webhook entry is enabled, a legacy active Execution exists, or a
RabbitMQ active Attempt exists, the following are forbidden:

- changing code, dependencies, run parameters or Credential bindings;
- changing the Worker, Task run mode, Cron, Webhook path or Token;
- saving and deleting the Adapter.

A plain `queued/retry_wait` Execution already owns an immutable snapshot and does not
by itself lock the current InputConfig; later saves affect only new Executions. Name
and description remain editable. The frontend must explain the disabled reason; the
backend keeps the stable 409 error as the final gate.

## 6. Live Logs and History

All trigger modes reuse the Execution SSE and the same watcher:

- Task shows the bottom live log automatically after clicking Run;
- When a Schedule Execution starts, the UI hints and follows that log without
  switching away from the history detail the user is viewing;
- Webhook shows “Waiting for Webhook request…” once enabled, and automatically tracks
  the Execution created by a real request;
- The log workspace supports bottom, fullscreen and restore-to-bottom modes;
- After an Execution reaches a terminal state, full details remain available from
  Execution History or Call History.

## 7. Clone Upgrade

Upgrading while running uses Clone:

```text
Copy Adapter
→ modify and save the new Adapter
→ optional verification
→ stop the old Adapter
→ run the new Adapter
→ delete the old Adapter after verification
```

Clone copies language, code, dependencies, run parameters, Credential references,
trigger configuration and the run node; it does not copy Execution history, and the
new Adapter always starts stopped.

A Webhook Clone may use the same path as its source Adapter, but only one of them can
receive at a time, so the external URL can stay unchanged while an operator stops the
old one and starts the new one.

## 8. Deletion

The user action is always “Delete Adapter”. A running Adapter must be stopped first;
after a successful delete the Adapter disappears from the active Catalog. The backend
keeps a soft-delete fact as the boundary for a future, independently designed recycle
bin, but the current product offers no recycle bin entry.

## 9. Runtime Contract

All three languages share:

```text
Input → handle(context, input) → Output
```

- Python: `def handle(context, input)`;
- JavaScript: `export async function handle(context, input)`;
- Java: a fixed `Adapter` class with `handle(Context context, Object input)`;
- `context.config` provides non-sensitive run parameters;
- `context.secrets.get(key)` provides bound credentials;
- `context.logger` emits live logs.

The RabbitMQ v3 path adds `queued / running / retry_wait / dead_letter` to the
existing terminal states. Worker ACKs after the durable Control Claim commits and
its private journal is atomically persisted, then enters the resource Sandbox. A
post-ACK crash is recovered through Attempt Lease/Fencing and a new generation, not
by relying on the original message redelivery. Adapter side effects against external
systems still need business idempotency keys.

The Task Starter Code logs “Task started / Task finished” and the Webhook Starter
Code logs “Webhook request received / Webhook request processed” (in `zh-CN`:
“任务开始 / 任务结束” and “收到 Webhook 请求 / 处理完 Webhook 请求”). When a new
Adapter is created, the Starter Code comments and example platform logs follow the
current system language (`zh-CN` Chinese / `en` English); existing Adapter code is
never rewritten by a language switch and no new Revision is created.

## 10. AI Assistant Boundaries

The AI Assistant can read the current Working Copy and minimal non-sensitive context,
return a complete Candidate and provide a Diff. One-request attachments support PDF,
DOCX, XLS, XLSX, text, code, and supported images. Spreadsheet extraction is bounded
and in-memory; it never evaluates formulas, macros, or external links. Current Managed
Input contributes only ordered safe labels and the three-language
`context.input_files` / `context.inputFiles` contract: AI never reads its Blob and
must not claim to know content that was not separately attached. Apply only updates
the browser Working Copy; it does not save, run, or modify Credential true values or
run state. Prompts, raw Provider responses, reasoning, attachment bodies, and
conversations are never persisted.

## 11. Security Principles

- Adapter code must not hard-code passwords, Tokens or private keys;
- Credential true values are never returned to the browser, written to logs, or put
  into AI Prompts;
- Webhook Bearer Tokens are compared in constant time;
- Runtime logs are redacted against the injected Secret set;
- DLR remains a trusted-administrator code model. The Linux cgroup v2 Sandbox bounds
  resources and processes but is not a security boundary for untrusted tenants;
- the default single-node RabbitMQ deployment is not HA; Quorum Queue durability is
  not a substitute for multi-node disaster tolerance.

## 12. System Language and Display Names

The “Language” entry in System Settings is a deployment-level system language,
default `zh-CN`, switchable to `en`:

- It is changed by an administrator and persisted as the authenticated authority;
  the Console and built-in Ant Design copy follow it. Unauthenticated login uses a
  separate browser preference (first visit defaults to `zh-CN`) and restores the
  system language immediately after authentication;
- A new Execution captures the system language at creation time and keeps it fixed
  for its whole lifecycle, regardless of later switches;
- Stable backend errors keep `error code + structured params` as the machine
  contract; the frontend localizes the display per the current language, keeping the
  existing message as a compatible fallback;
- System preset content (package sources, Credential types, etc.) shows localized
  names per language while internal codes / IDs stay unchanged; business logic never
  depends on display names;
- User-created Adapter names, descriptions, Credential names, etc. are never
  auto-translated;
- A language switch never modifies existing code, never creates a new Revision, and
  never rewrites historical Execution logs in bulk.

## 13. Explicitly Not Implemented

No Adapter chaining, DAG, synchronous Webhook invoke, URL takeover, resident Adapter,
RBAC, AI auto-execution loop, generic plugin framework, unified Sink, user-level
language preference, machine translation of user content, a third language, or a
RabbitMQ HA cluster. Reliable Runtime retry is a bounded recovery contract for one
Execution, not general workflow orchestration.

## 14. Current Completion Criteria

A technical user who does not know the internal Revision implementation can complete
the following without consulting the docs:

```text
Task / Webhook creation
→ edit and save
→ choose a run node
→ run or start receiving
→ view live logs and history
→ stop
→ Clone upgrade
→ delete the old Adapter
```

Alternatively, start from a Recipe:

```text
choose a scenario and language in Template Gallery
→ name and copy it
→ edit the independent Adapter immediately
→ configure Worker / Dependency / Input / Credential / Endpoint
→ save, preview, and decide on use from the actual maturity evidence
```

## 15. Reliable Runtime Operational Boundary

- Defaults keep ordinary RabbitMQ ingress off, legacy Claim on, and all three
  Cutover attestations off. An ordinary install never performs Final Cutover
  automatically.
- Final Cutover is a staged administrator operation: prove backup/restore, drain or
  migrate legacy work, require Worker v3 plus the Linux Sandbox, enable ordinary
  traffic, pressure-test Slot authority, require minimum protocol 3, retire the old
  index, and only then close legacy Claim. The order is not interchangeable.
- Post-Cutover rollback uses a compatible Control that understands the additive
  schema to drain and repair. Never start an old binary against new rows or treat a
  production `alembic downgrade` as recovery.
- See [Reliable Runtime migration notes](issue130-reliable-runtime-migrations.md) for
  configuration, read-only inventory/preflight/invariant APIs, and failure handling;
  see [Sandbox deployment](issue130-sandbox-deployment.md) for Linux prerequisites.
