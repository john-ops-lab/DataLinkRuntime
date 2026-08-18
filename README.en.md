<h1 align="center">DataLinkRuntime</h1>

<p align="center">
  A lightweight, self-hostable data adapter runtime for building, running and operating data integration code in the browser.
</p>

<p align="center">
  <a href="https://github.com/john-ops-lab/DataLinkRuntime/actions/workflows/ci.yml"><img src="https://github.com/john-ops-lab/DataLinkRuntime/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/Python-3.13-blue" alt="Python 3.13">
  <img src="https://img.shields.io/badge/Runtime-Python%20%7C%20JavaScript%20%7C%20Java-informational" alt="Runtimes">
</p>

<p align="center">
  <a href="README.md">简体中文</a> · <strong>English</strong> ·
  <a href="docs/en/product.md">Product</a> ·
  <a href="docs/en/architecture.md">Architecture</a> ·
  <a href="docs/specs/README.md">Specs</a> ·
  <a href="https://github.com/john-ops-lab/DataLinkRuntime/issues/80">Roadmap</a>
</p>

# DataLinkRuntime - Lightweight Data Adapter Runtime

DataLinkRuntime (DLR) is designed for data collection, receiving, parsing, transformation and output in CMDB and other integration scenarios.

The core idea is simple: an **Adapter is a self-contained data processing unit**. You write code in the Web Workbench, DLR runs it on a Worker, and every execution has traceable input, output, logs and status.

DLR is intentionally not a workflow engine or a low-code platform. It focuses on keeping Adapter development, execution and operations small, explicit and easy to self-host.

## Main Concepts

1. **Create an Adapter**
   - `Task`: run manually or on a schedule.
   - `Webhook`: receive external JSON requests through an HTTP endpoint.

2. **Develop in the browser**
   - Monaco-based editor.
   - Python, JavaScript and Java share one `Input → handle(context, input) → Output` contract.
   - Dependencies, runtime settings and Credential bindings stay with the Adapter.

3. **Run on Workers**
   - Control manages state and scheduling but never executes user Adapter code.
   - Workers claim executions and run code in fresh subprocesses / JVMs.
   - One Adapter has at most one active `pending / running` Execution.

4. **Observe every run**
   - Live stdout / stderr.
   - Structured Output.
   - Execution history and Webhook call history.
   - Timeout, cancellation and runtime-lock semantics are shared across trigger types.

5. **Use AI as a human-in-the-loop coding assistant**
   - The AI Assistant reads the current Working Copy and bounded non-sensitive context.
   - It returns a complete Candidate snapshot.
   - You review the Diff and explicitly Apply it.
   - Apply never automatically saves, tests or runs the Adapter.

## Features

- Browser-based Adapter Workbench with Monaco Editor.
- Task manual run and Cron / Timezone scheduling.
- Webhook Adapter with Bearer Token authentication.
- Python 3.13, JavaScript / Node.js and Java 21 runtimes.
- Version-scoped dependency environments.
- PostgreSQL-backed Execution state and scheduling.
- Worker heartbeat and capability-aware dispatch.
- Live logs, Execution history and Webhook call history.
- Encrypted Credentials and Adapter Secret bindings.
- Package source management for Python / npm / Maven.
- Human-in-the-loop AI Assistant with Candidate → Diff → Apply.
- Deployment-level `zh-CN / en` internationalization.
- Docker Compose self-hosting.

## Quick Start

Prerequisite: Docker with Compose v2.

Create the deployment configuration and replace the placeholder secrets:

```bash
cp .env.example .env
# Edit .env and set at least:
# DLR_ADMIN_TOKEN
# DLR_WORKER_TOKEN
# DLR_MASTER_KEY
```

Start PostgreSQL first and apply migrations:

```bash
docker compose up -d postgres
docker compose run --rm control alembic upgrade head
```

Start the full stack:

```bash
docker compose up -d --build
```

Check health:

```bash
docker compose ps
```

When all services are healthy:

- Web Console: `http://localhost:8080`
- Health API: `http://localhost:8080/api/health`

The first Web Console visit asks for `DLR_ADMIN_TOKEN`; it is kept in browser `sessionStorage` only.

To remove the local stack and database volume:

```bash
docker compose down --volumes
```

## Architecture

```text
┌─────────────────────┐
│ Web                  │
│ React + Monaco       │
└──────────┬──────────┘
           │ HTTP/JSON + SSE
           ▼
┌─────────────────────┐       ┌─────────────────────┐
│ Control              │──────▶│ PostgreSQL          │
│ FastAPI              │       │ state / scheduling  │
│ API / gates / AI     │       │ execution history   │
└──────────┬──────────┘       └─────────────────────┘
           │ Worker long polling
           ▼
┌─────────────────────┐
│ Worker               │
│ Python / Node / Java │
│ subprocess execution │
└─────────────────────┘
```

DLR keeps the execution boundary explicit:

- **Web** provides the operator experience.
- **Control** owns APIs, transactions, scheduling, Webhook routing and AI Provider integration.
- **PostgreSQL** is the durable source of truth.
- **Worker** is the only component that runs user Adapter code.

See [Overall Architecture](docs/en/architecture.md) for the current detailed contract.

## Stack

| Layer | Technology |
|---|---|
| Web | React 19, TypeScript, Vite, Ant Design, Monaco Editor, i18next |
| Control | Python 3.13, FastAPI, SQLAlchemy 2, Alembic |
| Database | PostgreSQL 16 |
| Worker | Python, Node.js / npm, JDK 21 / Maven |
| Python tooling | uv, pytest, Ruff, mypy |
| Deployment | Docker Compose |

## AI Assistant

The current AI Assistant is a constrained coding assistant, not an autonomous Agent.

```text
Working Copy + bounded context
→ model response
→ strict Candidate validation
→ Diff review
→ explicit Apply
→ browser Working Copy becomes dirty
```

Security and behavior boundaries:

- The Working Copy is the authoritative code snapshot for the request.
- Credential true values never enter the Prompt.
- Only bound Secret key names may be exposed to help the model generate usable code.
- Provider reasoning is not persisted or displayed.
- AI conversations, Prompts and raw Provider responses are not persisted.
- Apply never performs Save / Test / Run automatically.

M5.7 is extending this UI with `assistant-ui`, Regenerate, attachments, controlled read-only Tool Calls and MCP knowledge access. These are roadmap items until their implementation and acceptance are complete. See [Issue #80](https://github.com/john-ops-lab/DataLinkRuntime/issues/80).

## Security

- Credentials are encrypted at rest using a key derived from deployment-level `DLR_MASTER_KEY`.
- Credential plaintext is never returned to the browser.
- Runtime Secrets are injected only for the target Execution and are redacted from platform logs.
- Admin and Worker APIs use separate Bearer Tokens.
- Webhook Tokens use constant-time comparison.
- Control never executes user Adapter code.
- DLR v1 uses a trusted-administrator code model; subprocess isolation is **not** a security sandbox.

Do not hard-code passwords, Tokens, private keys or other Secrets in Adapter source code.

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

## Documentation

- [Product Definition](docs/en/product.md)
- [Overall Architecture](docs/en/architecture.md)
- [Specification Index and precedence rules](docs/specs/README.md)
- [M5.7 AI Assistant Spec](docs/specs/m5-7-ai-assistant.md)

Historical M1-M4 specs are retained for traceability. When documents conflict, follow the precedence rules in `docs/specs/README.md` rather than treating every historical spec as current behavior.

## Roadmap

Current stage: **M5.7 - AI Assistant UI componentization and controlled knowledge access**.

Planned scope includes:

- `assistant-ui` based chat UI.
- Regenerate.
- Image, PDF, Word, text and code attachments.
- Provider-native file / multimodal capability first, with bounded DLR fallback parsing.
- Read-only Tool Call support.
- MCP knowledge access, with Tencent ima Knowledge Base as the first POC.

Streaming token output, reasoning UI and a general-purpose autonomous Agent Runtime are explicitly outside M5.7.

Track the current contract in [Issue #80](https://github.com/john-ops-lab/DataLinkRuntime/issues/80).

## License

DataLinkRuntime is open source under the [MIT License](LICENSE).

Copyright (c) 2026 john-ops-lab.
