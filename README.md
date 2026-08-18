# DataLinkRuntime (DLR)

**English** | [简体中文](README.zh-CN.md)

A lightweight data adapter runtime platform for data collection, receiving, parsing,
transformation and output for CMDB and other systems.

- Product definition: [docs/en/product.md](docs/en/product.md)
- Overall architecture: [docs/en/architecture.md](docs/en/architecture.md)
- M4 AI Editor Spec: [docs/specs/m4-ai-editor.md](docs/specs/m4-ai-editor.md)

The M5.4 Workbench is complete: Python / JavaScript / Java Adapters share immutable
Revisions, Secrets, live logs and Execution history semantics, and the right-hand
AI Assistant can generate a complete Candidate from the current Working Copy. A
Candidate must be reviewed as a Diff and explicitly applied by an administrator;
applying only updates the browser Working Copy; saving and running always remain
administrator actions. Task Adapters support manual or scheduled runs, and both
entries always execute the latest saved content and are pinned to the configured
runtime node. Webhook Adapters automatically receive a random URL path at creation;
after binding a Token Credential and a runtime node they can start receiving. Every
successful JSON request asynchronously creates one Execution, and each Adapter keeps
only the most recent 100 Webhook call records. Stopping reception immediately rejects
new requests but does not terminate calls that are already executing. Task, Schedule
and Webhook reuse the unified run lock and the Workbench bottom live log; the log
supports fullscreen and restore. While running, code, run configuration, saving and
deletion are locked, with a Clone upgrade entry and an explicit reason. The official
Worker image includes Python 3.13 / uv, Node.js LTS / npm and JDK 21 / Maven, and
reports capability from the runtimes actually available. M4.1 further derives the
Worker effective online status from heartbeat timeout so run entries never pick a
Worker that has already lost contact.

## Quick Start

Prerequisites: Docker (with Compose v2).

Since M2, Control / Worker require a static Token; since M3.2 the Secret Store also
requires the deployment-level `DLR_MASTER_KEY`. Compose does not ship usable defaults
for any of these values:

```bash
cp .env.example .env   # replace placeholder values such as DLR_ADMIN_TOKEN / DLR_WORKER_TOKEN / DLR_MASTER_KEY
docker compose up -d --build
```

On first start the Worker needs the `workers` table to register, so the database
migration must run first:

```bash
# wait for PostgreSQL to start
docker compose ps postgres
docker compose run --rm control alembic upgrade head
```

After the migration, wait until all services are healthy:

```bash
docker compose ps
```

Once all services report `healthy`:

- Web UI: http://localhost:8080 (the first visit asks for `DLR_ADMIN_TOKEN`, stored only in the browser sessionStorage)
- Control health (via web/nginx): http://localhost:8080/api/health
- Worker: no external port (outbound long polling to Control; healthcheck based on the ready file)

Admin APIs require `Authorization: Bearer <DLR_ADMIN_TOKEN>`; Worker APIs require
`DLR_WORKER_TOKEN`. Runtime Secrets are injected into the Worker only, as
`DLR_SECRET_*` entries, and Adapters read them via `context.secrets.get(...)`.

Clean up the environment (including the database volume):

```bash
docker compose down --volumes
```

## Components

| Component | Description |
|-----------|-------------|
| web | React + TypeScript + Vite SPA, served by Nginx which proxies `/api` |
| control | FastAPI control node (Python 3.13) |
| postgres | PostgreSQL 16 |
| worker | Worker Agent: register / heartbeat / long polling, executes Adapters in separate sub-processes of a version-scoped environment per language |

## Worker Effective Online Status

Workers send a heartbeat every 10 seconds by default
(`DLR_WORKER_HEARTBEAT_SECONDS`). `workers.status` in the database is the Stored
Status that a Worker writes actively at register / heartbeat / graceful offline;
Control never rewrites expired heartbeats to `offline` through a background task.

When Control needs to decide whether a Worker is currently usable, it derives the
Effective Status: the Stored Status must be `online` and the latest heartbeat age
must be less than or equal to `DLR_WORKER_HEARTBEAT_TIMEOUT_SECONDS` (default 30
seconds). Exactly at the timeout boundary the Worker still counts as online. The
`status` of the Admin Worker API, Test and Start all use this effective status;
`last_heartbeat` is returned as-is for troubleshooting only, and the Web never
recomputes it with browser time.

The heartbeat timeout must be positive and strictly greater than the Worker
heartbeat interval; when adjusting the heartbeat interval, adjust the timeout
accordingly — about 3 times the interval is recommended. An expired Worker
automatically returns to effective online once heartbeats resume, with no manual
intervention.

## AI Assistant Configuration and Boundaries

The AI Assistant uses one global active model configuration. In
「System Settings → AI Model」, select a Provider and fill in the Base URL and Model
ID; the Model ID can be refreshed from the Provider's `/v1/models` or entered
manually. If an API Key is needed, create a `token`-type Credential first and
reference it in the AI settings; the browser and the AI settings API only see
Credential metadata, never the plaintext token. The reasoning strategy defaults to
「Follow model default」, in which case DLR does not send a reasoning override.

The default timeout for non-streaming Provider HTTP requests is 180 seconds and can
be adjusted with `DLR_AI_PROVIDER_TIMEOUT_SECONDS` in the 10–600 second range; this
parameter only controls the request deadline and does not add streaming or output
token management.

When using AI features, the current Adapter code, ordinary configuration and a
bounded recent conversation are sent to the configured model service to generate
suggestions; the names of bound Secrets are forwarded (so the model can produce
usable code), but the true values of passwords, Tokens, keys and other sensitive
credentials are never sent to the model, and the browser and the AI settings API
never return those true values. The selected model API Key is used only for request
authentication and never enters the Prompt. Conversations, Prompts, Provider
Responses and reasoning are not persisted and are not written to ordinary application
logs. Candidate changes returned by the model still require human review and Apply
after local Schema validation; Apply does not save, test or run the Adapter.

## Local Development

### backend

Prerequisites: [uv](https://docs.astral.sh/uv/) (it installs Python 3.13
automatically).

```bash
cd backend
uv sync --frozen
uv run uvicorn dlr.control.app:create_app --factory --reload
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest   # needs a reachable PostgreSQL (tests create their own isolated dlr_test database and run real migrations)
```

If no PostgreSQL is available locally, start a temporary test instance (the default
`DATABASE_URL` connects to it directly):

```bash
docker run --rm -d --name dlr-dev-pg -p 127.0.0.1:5432:5432 \
  -e POSTGRES_USER=dlr -e POSTGRES_PASSWORD=dlr -e POSTGRES_DB=dlr postgres:16
```

Database migrations (PostgreSQL is reachable only inside the Compose network):

```bash
docker compose run --rm control alembic upgrade head
```

### web

Prerequisites: Node.js 22+.

```bash
cd web
npm ci
npm run dev        # http://localhost:5173, /api proxied to localhost:8000
npm run lint
npm run typecheck
npm run test
npm run build
```

## Smoke Test

Build and start all containers (an isolated compose project with dedicated ports),
run the Alembic migrations on a real PostgreSQL, wait for all services to be healthy,
then verify the `/api/health` chain and 401 authentication rejection. The M5.4 Task
main path really runs `Task create → Save + Worker → Run Once → succeeded → switch to
scheduled run → configure a short-period Schedule → enable → schedule Execution
succeeded → disable`, and verifies that Python / JavaScript / Java real base
executions all print “任务开始/任务结束”, that Run Once and Schedule both pin the
latest saved content / running Worker, the unified active lock, and that Clone leaves
the Schedule disabled. The Webhook main path really runs
`create → Save + Worker + Token → readable path → start receiving → POST JSON → 202 →
succeeded → call record → stop receiving`, and verifies that stopping does not
terminate an active Execution, the single in-flight constraint for the same path, and
`Clone disabled → old Adapter stopped → Clone takes over the original URL`. The M4
path additionally starts a temporary OpenAI-compatible fake Provider that lives only
in the smoke network, verifying settings, model refresh, connection test and
three-language AI Assist, and proving that AI does not change save, Execution or run
configuration facts; no public AI is accessed and the fake Provider never enters the
formal Compose topology. The whole process uses a dedicated Compose project and
volumes and cleans up afterwards; it also verifies the three default dependency
sources shipped with a fresh deployment, the restore-defaults API, and that the
default DNS fallback container configuration and Docker internal service-name
resolution do not regress:

```bash
./scripts/compose-smoke.sh
```

## Container Network and DNS Troubleshooting

### Default Behavior (M5.5.8)

The `control` / `worker` DNS list defaults to
`127.0.0.11 → 1.1.1.1 → 8.8.8.8`: the first entry is the Docker built-in resolver
(responsible for internal service names such as `postgres` / `control`), and the
other two are public DNS fallbacks tried only when the built-in resolver cannot
resolve public domains (corporate networks / VPN / firewalls blocking host DNS
forwarding). Only two kinds of external connections need public domain resolution:

- `control` accessing the AI Provider Base URL (the model service in AI settings);
- `worker` downloading dependencies per Adapter language (PyPI / npm / Maven; the
  default sources are configured in System Settings, with optional compatible
  configuration in `.env.example`).

### Overriding or Disabling DNS Fallback

- **Corporate / VPN / private DNS**: specify your own DNS in `.env`, for example
  ```bash
  DLR_DNS_FALLBACK_1=10.0.0.53
  DLR_DNS_FALLBACK_2=
  ```
- **Disable public DNS fallback completely** (back to the old default of pure Docker
  built-in resolver forwarding the host `resolv.conf`): leave both fallbacks empty:
  ```bash
  DLR_DNS_FALLBACK_1=
  DLR_DNS_FALLBACK_2=
  ```
- **Replace the whole DNS list** (without editing `docker-compose.yml`, requires
  Compose v2.24+):
  ```bash
  cp docker-compose.dns.example.yml docker-compose.dns.yml
  # edit docker-compose.dns.yml and replace it with DNS actually usable in your network
  docker compose -f docker-compose.yml -f docker-compose.dns.yml up -d --build
  ```

In every case `127.0.0.11` (the Docker built-in resolver) must stay first in the
list, or internal service names cannot be resolved inside containers.

### Layered Troubleshooting Order (DNS → TCP → TLS/HTTP)

Work bottom-up when troubleshooting; do not skip layers:

1. **DNS resolution failure**: the AI settings return error code
   `ai_provider_dns_failed` (the message contains 「域名解析失败」). Verify resolution
   inside the container directly:
   ```bash
   docker compose exec control python -c "import socket; socket.getaddrinfo('api.example.com', 443)"
   ```
   Failure means the DNS layer is broken: prefer the DNS override file above; on
   corporate networks confirm the DNS allows outbound resolution, and on VPN confirm
   routing does not block DNS traffic.
2. **TCP connection failure**: error code `ai_provider_unreachable` (the message
   contains 「TCP 连接或 TLS 握手失败」). Verify the three-way handshake to the target
   port:
   ```bash
   docker compose exec control python -c \
     "import socket; socket.create_connection(('api.example.com', 443), timeout=5)"
   ```
   Failure means the network layer is broken: check container outbound firewall /
   proxy / VPN routing; DNS is not involved.
3. **TLS / HTTP failure**: still `ai_provider_unreachable` (TLS handshake failure)
   or another error code (`ai_auth_failed` means credentials were rejected,
   `ai_model_not_found` means the Model ID does not exist, `ai_timeout` means the
   request timed out). This step means network and resolution are both fine; the
   problem is the server-side interface, certificate chain or authentication.

All three layers can be run at once inside a container (same DNS environment as
`control`):

```bash
# run the layered diagnostics inside the control container (DNS → TCP → TLS → HTTP)
docker compose exec -T control python - < scripts/diag-network.py --url https://api.example.com
```

The script stops at the failed layer and reports it with the exit code (2=DNS,
3=TCP, 4=TLS, 5=HTTP); on the host it can also run directly as
`python3 scripts/diag-network.py --host api.example.com --port 443` (the `--host`
mode only checks DNS/TCP; add `--tls` to also check the TLS handshake; no HTTP
probing).

### Docker Desktop / VPN / Enterprise Network Checklist

- Docker Desktop: confirm 「Settings → Resources → Network」 does not restrict
  outbound traffic and the host DNS works (`scutil --dns` / `nslookup` resolve the
  target domain).
- VPN: confirm the VPN does not blackhole all Docker VM traffic (temporarily
  disconnecting the VPN can reproduce the difference); if the VPN brings its own
  DNS, write it into `docker-compose.dns.yml`.
- Corporate network: confirm the proxy configuration; `control` outbound traffic
  does not use host proxy environment variables (`HTTP_PROXY` is not set in the
  image); if a corporate proxy is required, inject it at the deployment layer and
  keep the platform configuration file free of proxy credentials.
- The platform itself never exposes Token / Credential / Provider API Keys: all
  diagnostic commands use only domains, ports and URLs, and never read or echo
  secrets.
