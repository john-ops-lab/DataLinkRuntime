# Issue #117 Batch 1 PostgreSQL Compose evidence

- Generated: 2026-08-24T09:43:27Z
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

- `docker compose -f docker-compose.yml config --quiet`
- `docker compose -f docker-compose.yml -f docker-compose.dns.example.yml config --quiet`
- `COMPOSE_POSTGRES_REGRESSION_ID=issue117-b1-data-check ./scripts/compose-postgres-init-health.sh`
- `COMPOSE_SMOKE_PROJECT=dlr-i117-b1-smoke-20260824-r2 COMPOSE_SMOKE_WEB_PORT=8892 COMPOSE_SMOKE_ACCOUNT_WEB_PORT=8893 ./scripts/compose-smoke.sh`

The recorded scenarios contain no credentials or raw service payloads. The healthy scenario records the target `dlr` database query gate and Control start; the three failure scenarios record the expected PostgreSQL/Control states. The two init-time failure scenarios also assert that the PostgreSQL data volume remains uninitialized.
