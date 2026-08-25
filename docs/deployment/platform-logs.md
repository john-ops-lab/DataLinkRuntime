# Platform service logs (M5.11 Wave B)

DLR keeps platform-service logs separate from Adapter Execution logs. Execution
input/output/error/stdout/stderr remain in PostgreSQL and are governed by the
Execution retention policy; platform logs are operational diagnostics only.

## Choose the host-side root

For local development, use a repository-relative directory that the current
user can write:

```dotenv
DLR_PLATFORM_LOG_ROOT=./platform-logs
```

Linux production uses a separate absolute path on a persistent disk:

```dotenv
DLR_PLATFORM_LOG_ROOT=/var/lib/dlr/platform-logs
```

These are host-side paths selected by `DLR_PLATFORM_LOG_ROOT`; the application
path inside every container remains `/var/lib/dlr/platform-logs`. Before local
Compose startup, prepare all five directories:

```bash
LOG_ROOT=./platform-logs
mkdir -p "$LOG_ROOT"/{control,worker,web,account-web,postgres}
```

## Compose layout

Set `DLR_PLATFORM_LOG_ROOT` to the host root prepared for the environment.
Compose binds the following five subdirectories into the services:

```text
$DLR_PLATFORM_LOG_ROOT/
├── control/       control application and Uvicorn logs
├── worker/        Worker agent logs
├── web/           token-entry Nginx access/error logs
├── account-web/   account-entry Nginx access/error logs
└── postgres/      PostgreSQL collector logs
```

For Linux production, create `/var/lib/dlr/platform-logs` and its five
subdirectories before deployment. The `postgres/` directory must be writable
by the PostgreSQL container's `postgres` user: inspect `id postgres` in the
pinned image and grant only the minimum required ownership/permissions to that
directory. The startup entrypoint checks this write access before `initdb` and
refuses to continue when the directory is missing or unwritable. Do not use `chmod 777`.

The bind mount is not a Docker named volume, so `docker compose down -v` does
not remove these host files. Only the host side is selected by the variable;
the application path inside containers is fixed at
`/var/lib/dlr/platform-logs`.

Every Compose service uses Docker's bounded `local` logging driver as a recent
stdout/stderr fallback (`max-size: 10m`, `max-file: 3`). `docker compose logs`
therefore remains useful for startup failures, but the bind-mounted files are
the persistent platform-log source of truth.

Nginx's persistent access format deliberately records the method and URI
without query strings, request bodies, `Authorization`, `Cookie`, or referer
headers. DLR application logs must likewise contain metadata and diagnostics;
application redaction excludes credential values, full Adapter input/output,
source code, and secrets.

## External rotation

Install `deploy/logrotate/dlr-platform-logs.conf` as
`/etc/logrotate.d/dlr-platform-logs` and run a dry run before enabling it:

```sh
sudo install -m 0644 deploy/logrotate/dlr-platform-logs.conf \
  /etc/logrotate.d/dlr-platform-logs
sudo install -m 0755 deploy/logrotate/dlr-platform-logs-postrotate.sh \
  /usr/local/sbin/dlr-platform-logs-postrotate
sudo install -d -m 0755 /etc/default
sudo sh -c 'cat > /etc/default/dlr-platform-logs <<\EOF
DLR_COMPOSE_PROJECT=dlr
DLR_COMPOSE_FILE=/opt/dlr/docker-compose.yml
DLR_POSTGRES_USER=dlr
DLR_POSTGRES_DB=dlr
EOF'
sudo logrotate -d /etc/logrotate.d/dlr-platform-logs
sudo logrotate -f /etc/logrotate.d/dlr-platform-logs
```

The supplied policy checks daily, rotates when a file exceeds 50 MiB, keeps
14 compressed rotations/days, and creates mode `0640` files for application and
Nginx logs. PostgreSQL is intentionally not given a host-side `create` rule:
the postrotate helper asks the collector to create its next file with the
container user's own `0600` ownership. Adjust the root paths in the installed
copy when `DLR_PLATFORM_LOG_ROOT` is overridden. The postrotate helper must be
able to reach the Compose project:
it reopens Nginx with `nginx -s reopen` and calls PostgreSQL's
`pg_rotate_logfile()`. Control and Worker use Python's `WatchedFileHandler` and
reopen on the next record. Do not use `copytruncate`; the explicit reopen
operations avoid leaving a process writing to a renamed inode. A safe
operational check is:

1. record the current inode and write one normal request/heartbeat;
2. run `logrotate -f` and confirm the old file is renamed/compressed;
3. write another request/heartbeat and confirm a fresh current file receives it
   (for PostgreSQL, verify that `pg_rotate_logfile()` returned `t`);
4. inspect ownership/mode and verify no credential or request body appears.

Remote Workers use the same directory and policy on their own host. This Wave
does not add shared storage or centralized log forwarding.

## Execution retention configuration

Control runs one retryable retention loop. It only deletes terminal rows, in
batches, and commits each successful batch. `pending` and `running` rows are
never selected. A policy applies both a creation-age cutoff and a maximum
terminal count per Adapter/trigger; deleting an Execution removes its stored
input, output, error, stdout and stderr with the row. The cycle logs only
counts, trigger/Adapter metadata, failures and elapsed time, never log text.

Defaults are intentionally bounded but different for the trigger families:

| Environment variable | Default | Meaning |
| --- | ---: | --- |
| `DLR_EXECUTION_RETENTION_WEBHOOK_DAYS` | `30` | Webhook terminal age |
| `DLR_EXECUTION_RETENTION_WEBHOOK_MAX_PER_ADAPTER` | `100` | Webhook terminal count |
| `DLR_EXECUTION_RETENTION_TASK_DAYS` | `30` | Task/manual terminal age |
| `DLR_EXECUTION_RETENTION_TASK_MAX_PER_ADAPTER` | `1000` | Task/manual terminal count |
| `DLR_EXECUTION_RETENTION_SCHEDULE_DAYS` | `90` | Schedule terminal age |
| `DLR_EXECUTION_RETENTION_SCHEDULE_MAX_PER_ADAPTER` | `1000` | Schedule terminal count |
| `DLR_EXECUTION_RETENTION_BATCH_SIZE` | `100` | Maximum rows per delete transaction |
| `DLR_EXECUTION_RETENTION_INTERVAL_SECONDS` | `3600` | Cleanup cycle interval |

PostgreSQL reuses deleted row space through normal maintenance; deletion does
not claim that the database file immediately shrinks. Monitor disk usage and
autovacuum, and schedule additional PostgreSQL maintenance according to the
deployment's own operations policy.
