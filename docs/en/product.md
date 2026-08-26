# DLR（DataLinkRuntime）Product Definition

> Current baseline: `v0.1.1` (including the Issue #117 manual-test fixes).
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
| Execution | One concrete run, recording input, output, stdout, stderr, status and duration |
| Worker | The node that actually runs user code, participating in scheduling by language capability |
| Credential | An encrypted credential; the browser never receives its true value |

Users only perform “Save”. The system creates an immutable Revision in the background
and pins later runs to the latest saved content.

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
creates an Execution and returns `202 + execution_id`; requests are not queued when
the same Adapter is busy.

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

An Adapter has at most one `pending / running` Execution at a time. Task manual runs,
Schedule and Webhook share this single lock.

While running or an entry is enabled, the following are forbidden:

- changing code, dependencies, run parameters or Credential bindings;
- changing the Worker, Task run mode, Cron, Webhook path or Token;
- saving and deleting the Adapter.

Name and description remain editable. The frontend must explain the disabled reason;
the backend keeps the stable 409 error as the final gate.

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

The Task Starter Code logs “Task started / Task finished” and the Webhook Starter
Code logs “Webhook request received / Webhook request processed” (in `zh-CN`:
“任务开始 / 任务结束” and “收到 Webhook 请求 / 处理完 Webhook 请求”). When a new
Adapter is created, the Starter Code comments and example platform logs follow the
current system language (`zh-CN` Chinese / `en` English); existing Adapter code is
never rewritten by a language switch and no new Revision is created.

## 10. AI Assistant Boundaries

The AI Assistant can read the current Working Copy and minimal non-sensitive context,
return a complete Candidate and provide a Diff. Apply only updates the browser
Working Copy; it does not save, run, or modify Credential true values or run state.
Prompt, raw Provider responses, reasoning and conversations are never persisted.

## 11. Security Principles

- Adapter code must not hard-code passwords, Tokens or private keys;
- Credential true values are never returned to the browser, written to logs, or put
  into AI Prompts;
- Webhook Bearer Tokens are compared in constant time;
- Runtime logs are redacted against the injected Secret set;
- v1 is a trusted-administrator code model; sub-process isolation is not a security
  sandbox.

## 12. System Language and Display Names

The “Language” entry in System Settings is a deployment-level system language,
default `zh-CN`, switchable to `en`:

- It is changed by an administrator and persisted as the authoritative value; it
  takes effect immediately, and the login page and built-in Ant Design copy follow it;
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

No Adapter chaining, DAG, synchronous Webhook invoke, request queue, automatic retry,
URL takeover, resident Adapter, RBAC, AI auto-execution loop, generic plugin
framework, unified Sink, user-level language preference, machine translation of user
content, or a third language.

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
