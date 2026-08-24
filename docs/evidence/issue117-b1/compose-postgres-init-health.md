# Issue #117 Batch 1 PostgreSQL Compose evidence

- Generated: 2026-08-24T10:02:01Z
- Image source: docker/postgres.Dockerfile

## missing-log-directory

- Compose up exit: 1
- PostgreSQL state: exited
- PostgreSQL health: unhealthy
- Control marker: not-started
- Data volume init: not-initialized
- Relevant log: postgres-1  | PostgreSQL startup blocked: platform log directory does not exist: /var/lib/dlr/platform-logs/postgres

## unwritable-log-directory

- Compose up exit: 1
- PostgreSQL state: exited
- PostgreSQL health: unhealthy
- Control marker: not-started
- Data volume init: not-initialized
- Relevant log: postgres-1  | PostgreSQL startup blocked: platform log directory is not writable by postgres: /var/lib/dlr/platform-logs/postgres

## missing-target-database

- Compose up exit: 1
- PostgreSQL state: running
- PostgreSQL health: unhealthy
- Control marker: not-started
- Data volume init: not-checked
- Relevant log:

## healthy

- Compose up exit: 0
- PostgreSQL state: running
- PostgreSQL health: healthy
- Control marker: started
- Data volume init: not-checked
- Relevant log:

## Verification commands

- `docker compose -f docker-compose.yml config --quiet` (pass with anonymous `EXAMPLE_DLR_*` values)
- `docker compose -f docker-compose.yml -f docker-compose.dns.example.yml config --quiet` (pass with anonymous `EXAMPLE_DLR_*` values)
- `docker run --rm --label ao.session=... postgres:16-alpine sh -c 'command -v su-exec'` (pass: `/usr/local/bin/su-exec`)
- `docker run --rm --label ao.session=... --entrypoint /bin/sh dlr-i117-b1-smoke-20260824-repair-postgres:latest -c "grep -n 'su-exec postgres test -w' /usr/local/bin/dlr-postgres-entrypoint.sh && command -v su-exec"` (pass: line 11 and `/usr/local/bin/su-exec`)
- `COMPOSE_POSTGRES_REGRESSION_ID='Issue.117/Upper' ./scripts/compose-postgres-init-health.sh` (pass; normalized RUN_ID: `issue-117-upper`)
- `COMPOSE_SMOKE_PROJECT=dlr-i117-b1-smoke-20260824-repair COMPOSE_SMOKE_WEB_PORT=8894 COMPOSE_SMOKE_ACCOUNT_WEB_PORT=8895 COMPOSE_SMOKE_TIMEOUT=300 ./scripts/compose-smoke.sh` (pass; complete Compose smoke)

The recorded scenarios contain no credentials or raw service payloads. The healthy scenario records the target `dlr` database query gate and Control start; the three failure scenarios record the expected PostgreSQL/Control states. The two init-time failure scenarios also assert that the PostgreSQL data volume remains uninitialized.
