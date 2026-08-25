<h1 align="center">DataLinkRuntime</h1>

<p align="center">
  <strong>Code your data connections.</strong>
</p>

<p align="center">
  Write your data connection logic as an <strong>Adapter</strong>, then run it directly.
</p>

<p align="center">
  From code editing and dependency configuration to execution, logs and history,<br>
  DataLinkRuntime provides a lightweight, self-hosted environment for developing and running data adapters.
</p>

<p align="center">
  <strong>Develop → Run → Observe</strong>
</p>

<p align="center">
  <a href="https://github.com/john-ops-lab/DataLinkRuntime/actions/workflows/ci.yml"><img src="https://github.com/john-ops-lab/DataLinkRuntime/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/Python-3.13-blue" alt="Python 3.13">
  <img src="https://img.shields.io/badge/Runtime-Python%20%7C%20JavaScript%20%7C%20Java-informational" alt="Runtimes">
</p>

<p align="center">
  <a href="README.md">简体中文</a> ·
  <strong>English</strong> ·
  <a href="docs/en/product.md">Product</a> ·
  <a href="docs/en/architecture.md">Architecture</a> ·
  <a href="docs/specs/README.md">Specs</a> ·
  <a href="https://github.com/john-ops-lab/DataLinkRuntime/issues">Issues</a>
</p>

---

## What is DataLinkRuntime?

DataLinkRuntime (DLR) is an **AI-assisted, code-first platform for developing and running data adapters**.

Many data integration tasks are fundamentally just a small amount of code:

```text
Read / Receive data
        ↓
Validate / Transform
        ↓
Apply business logic
        ↓
Send to the target system
```

The code itself may only be tens or hundreds of lines, but once it needs to run reliably over time, you also need dependency management, configuration, credentials, scheduling, versioning, logs, execution history and troubleshooting.

DLR brings those operational needs into one lightweight platform. Every **Adapter** is a self-contained data processing unit:

```text
Source / Event
      ↓
   Adapter
      ↓
Transform / Process
      ↓
    Target
```

The Adapter owns one complete unit of data processing and external output logic; DLR owns its development, execution and management environment.

---

## Code-first, AI-assisted

Many integration platforms reduce development effort by introducing components, nodes and visual workflows. DataLinkRuntime takes a different path:

> **Instead of decomposing logic into more and more platform-specific components, DLR lets AI help you generate and modify readable, runnable Adapter code directly.**

You can write code yourself or describe what you want:

```text
Describe → Generate → Review → Run
```

The DLR AI Assistant produces a Candidate. You review the Diff and explicitly Apply it. Apply only updates the browser Working Copy; it does not automatically save or run the Adapter.

Code remains the final asset, so it stays readable, editable, testable and versionable while retaining access to the existing Python, JavaScript and Java ecosystems.

**DLR does not try to eliminate code with more components. It uses AI to make code a simple way to express integration logic again.**

---

## Use cases

DLR is designed for **independent data connection tasks that need reliable execution and long-term maintainability**.

- **System-to-system synchronization**: read data from System A, transform it, then write it to System B.
- **API / data format adaptation**: field mapping, enum conversion, structure reshaping, data cleaning and protocol differences.
- **Data collection**: periodically collect data from cloud platforms, Kubernetes, VMware, databases, monitoring systems or business applications.
- **Webhook / event handling**: receive and transform events from GitHub, CI/CD, monitoring, cloud platforms or business systems.
- **Scheduled jobs and script consolidation**: bring scripts scattered across servers, Cron, containers or local directories into one runtime with logs and version history.
- **One-off data processing**: migrations, bulk corrections, temporary transformations and short-lived integration tasks.

Common processing patterns:

```text
Fetch → Transform → Push
```

or:

```text
Receive → Validate → Process → Send
```

---

## Core capabilities

| Capability | Description |
|---|---|
| **Web Workbench** | Create, edit and manage Adapters in the browser |
| **Multi-language Runtime** | Python, JavaScript and Java share one execution model |
| **Task** | Manual execution plus Cron / Timezone scheduling |
| **Webhook** | Receive external HTTP requests and create Executions |
| **Dependency management** | Manage Python, npm and Maven dependencies and package sources |
| **Credential** | Encrypt credentials and inject them through Secret Bindings |
| **Version traceability** | Every save produces an immutable runtime snapshot |
| **Live logs** | Observe Adapter execution logs in real time |
| **Execution history** | Inspect status, duration, input, output and logs for each Execution |
| **Worker Runtime** | Control and code execution are separated; Workers run Adapter code |
| **AI Assistant** | Human-in-the-loop Candidate → Diff → Apply coding assistance |
| **AI Context** | Explicit code / log context, attachments and controlled read-only knowledge sources |
| **Self-hosted** | Deploy the full platform on one server with Docker Compose |
| **Internationalization** | Simplified Chinese and English UI |

---

## Adapter Runtime Contract

All three languages share the same core model:

```text
Input → handle(context, input) → Output
```

| Language | Entry point |
|---|---|
| Python | `def handle(context, input)` |
| JavaScript | `export async function handle(context, input)` |
| Java | `Adapter.handle(Context context, Object input)` |

The runtime provides:

- `context.config`: non-sensitive runtime configuration;
- `context.secrets.get(key)`: access to bound Secrets;
- `context.logger`: real-time logging;
- `input`: JSON-compatible input;
- `output`: JSON-serializable output.

Adapters can call databases, HTTP APIs, SDKs or other external systems as needed.

---

## Adapter types

### Task

For actively executed data processing jobs. Task supports:

- manual execution;
- Cron / Timezone scheduling;
- custom Input;
- run / stop;
- live logs;
- execution history.

Typical flow:

```text
Create → Edit → Save → Run / Schedule → Observe
```

### Webhook

For data pushed from external systems:

```text
External System
      ↓
POST Webhook
      ↓
DLR Control
      ↓
Execution
      ↓
Worker
      ↓
Adapter
```

Useful for GitHub, CI/CD, monitoring, cloud platform and business-system events.

---

## AI Assistant

The AI Assistant is an Adapter development assistant, not an autonomous runtime Agent.

It can combine the current Working Copy with explicitly added code / log context, attachments and configured controlled read-only knowledge sources to help generate, modify and explain Adapter code.

```text
Working Copy + Context
        ↓
   AI Assistant
        ↓
     Candidate
        ↓
       Diff
        ↓
      Apply
        ↓
Working Copy (dirty)
```

AI does not automatically perform:

```text
Save
Run
Stop
Worker changes
Schedule / Webhook lifecycle changes
```

Saving and execution always require an explicit user action.

---

## Quick Start

### Prerequisites

Docker with Compose v2.

### 1. Create the deployment configuration

```bash
cp .env.example .env
```

Set at least:

```text
DLR_ADMIN_TOKEN
DLR_WORKER_TOKEN
DLR_MASTER_KEY
```

Replace all placeholder values with real random Secrets.

### 2. Prepare the platform log bind mounts

`.env.example` defaults to the repository-local, user-writable directory
`./platform-logs`, which differs from the absolute path
`/var/lib/dlr/platform-logs` used for Linux production deployments. Before
starting Compose, prepare five host subdirectories; Compose bind-mounts them to
the fixed container paths `/var/lib/dlr/platform-logs/<service>/`. The
repository-root `/platform-logs/` is exactly ignored by `.gitignore`; other
paths are not affected by that rule:

```bash
LOG_ROOT=./platform-logs
mkdir -p "$LOG_ROOT"/{control,worker,web,account-web,postgres}
```

The five directories are `control/`, `worker/`, `web/`, `account-web/` and
`postgres/`. Before PostgreSQL starts, the container's `postgres` user checks
that `postgres/` is writable. For Linux production, first run `id postgres` in
the pinned image and grant only the minimum required access to that directory.
Do not use `chmod 777`. If `DLR_PLATFORM_LOG_ROOT` changes, repeat the
preparation under the corresponding host root.

Platform logs use a separate bind mount: preserve the existing rotation and
redaction rules, and do not write tokens, Secrets, passwords or other real
credentials to `.env.example`, the log directories or command output. See the
[platform log deployment documentation](docs/deployment/platform-logs.md) for
the full production path, rotation and permission details.

### 3. Start PostgreSQL and apply migrations

```bash
docker compose up -d postgres
docker compose run --rm control alembic upgrade head
```

### 4. Start the full platform

```bash
docker compose up -d --build
```

Check service status:

```bash
docker compose ps
```

When all services are healthy:

- Web Console: `http://localhost:8080`
- Account Console: `http://localhost:8081`
- Health API: `http://localhost:8080/api/health`

Enter `DLR_ADMIN_TOKEN` on the first Web Console visit.

The account entry starts with `admin / admin123` and requires a password change.
Only a server-side password hash is stored. The 8080 Token entry and 8081
account entry share one Control service, PostgreSQL database and Web build; the
account host port can be changed with `DLR_ACCOUNT_WEB_HOST_PORT`.

Remove the local stack and database volume with:

```bash
docker compose down --volumes
```

---

## Architecture

```text
┌─────────────────────────────┐
│ Web                         │
│ React + Monaco + AI UI      │
└──────────────┬──────────────┘
               │ HTTP / JSON / SSE
               ▼
┌─────────────────────────────┐       ┌─────────────────────┐
│ Control                     │──────▶│ PostgreSQL          │
│ FastAPI                     │       │ State / History     │
│ API / Scheduler / AI        │       └─────────────────────┘
│ Webhook / Credential        │
└──────────────┬──────────────┘
               │ Worker Poll
               ▼
┌─────────────────────────────┐
│ Worker                      │
│ Python / Node.js / Java     │
│ Adapter Execution           │
└─────────────────────────────┘
```

Responsibilities:

- **Web**: Adapter development, configuration, execution and observability experience;
- **Control**: APIs, authoritative gates, scheduling, Webhook, Credential and AI Provider integration;
- **PostgreSQL**: authoritative platform state, versions and execution history;
- **Worker**: actual execution of user Adapter code.

Control itself never executes Adapter code.

See [Overall Architecture](docs/en/architecture.md) for the detailed contract.

---

## Product boundaries

DataLinkRuntime focuses on **developing and running independent Adapters**.

It is not a:

- DAG / Workflow orchestration engine;
- drag-and-drop low-code workflow platform;
- large-scale streaming compute platform;
- enterprise service bus;
- general-purpose Serverless platform;
- general-purpose autonomous AI Agent runtime.

If the core problem is multi-task dependency management, parallel branches, complex conditions, human approval or cross-task state orchestration, a dedicated Workflow / DAG platform is a better fit.

---

## Security boundaries

DLR currently uses a **trusted-code execution model**.

- Adapter subprocess isolation is not a security sandbox;
- do not execute arbitrary code from untrusted users;
- Credential plaintext is never returned to the browser;
- Secrets are injected only for the target Execution and redacted from platform logs;
- Control never directly executes user Adapter code;
- do not hard-code passwords, Tokens, private keys or other credentials in Adapter source code;
- the AI Assistant does not send Credential plaintext as normal model context;
- AI attachment content is sent to the model service configured by the administrator; do not upload passwords, keys or other sensitive credentials.

See [Overall Architecture](docs/en/architecture.md) for more security and runtime details.

---

## Technology stack

| Layer | Technology |
|---|---|
| Web | React 19, TypeScript, Vite, Ant Design, Monaco Editor, assistant-ui, i18next |
| Control | Python 3.13, FastAPI, SQLAlchemy 2, Alembic |
| Database | PostgreSQL 16 |
| Worker | Python, Node.js / npm, JDK 21 / Maven |
| Python tooling | uv, pytest, Ruff, mypy |
| Deployment | Docker Compose |

---

## Local Development

Backend:

```bash
cd backend
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Web:

```bash
cd web
npm ci
npm run lint
npm run typecheck
npm run test
npm run build
```

Full integration smoke test:

```bash
./scripts/compose-smoke.sh
```

The smoke test uses isolated local infrastructure and a fake AI Provider; it does not call a public AI service.

---

## Documentation

- [Product Definition](docs/en/product.md)
- [Overall Architecture](docs/en/architecture.md)
- [Specification Index and precedence rules](docs/specs/README.md)

Historical Specs are retained for design traceability. If historical documents conflict with current behavior, follow the precedence rules defined in `docs/specs/README.md`.

Report bugs, product issues or new use-case ideas through [GitHub Issues](https://github.com/john-ops-lab/DataLinkRuntime/issues).

---

## License

DataLinkRuntime is open source under the [MIT License](LICENSE).

Copyright (c) 2026 john-ops-lab
