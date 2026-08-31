# Issue #127 Managed Input operations

This runbook documents the Managed Input API, storage ownership, migration
compatibility, and a non-destructive rollback. Tokens, database URLs, and host
directories below are placeholders; never put real values in docs, logs, or
shell history.

## API and access boundary

Business users may read `GET /api/system/managed-input-capability`. Its response
contains only `managed_files_enabled`, `ready`, `default_retention_seconds`,
`max_custom_retention_seconds`, and `allow_manual_delete`. These are business-form
facts, not usage or deployment details. Only administrators may read or update
`/api/system/managed-input-settings`; that response exposes policy and usage but
never deployment paths, storage keys, tokens, secrets, or passwords.

Adapter input uses these boundaries:

| Operation | Method and path | Access/result |
| --- | --- | --- |
| Upload | `POST /api/adapters/{id}/input-artifacts` | Admin Bearer or account Cookie + CSRF; one multipart file |
| Staged list | `GET /api/adapters/{id}/input-artifacts` | Adapter edit |
| Save current input | `PUT /api/adapters/{id}/input-config` | Optimistic `expected_revision` |
| Staged/READY delete | `DELETE /api/adapters/{id}/input-artifacts/{artifact}` | Adapter edit; READY requires `expected_revision` |
| Artifact delete retry | `POST /api/system/managed-input-artifacts/{artifact}/retry-delete` | Administrator; thresholded `DELETE_FAILED` only |
| Deletion-job retry | `POST /api/system/managed-input-deletion-jobs/{job}/retry-delete` | Administrator; thresholded `DELETE_FAILED` only |

Clients branch on structured `detail.code`, never on message text. A `409`
`adapter_busy`, `input_config_revision_conflict`, or quota error keeps the draft
and refreshes it. `input_source_not_available` means the release flag is closed;
upload progress reports received bytes only.
Control renews an active upload writer periodically in the background; no
browser-callable renew API is exposed. Low-watermark and quota conflicts return
`409`; clients retain the draft and never display storage paths.

## Single Control and LocalFileArtifactStore

Only the Control process owns and writes `LocalFileArtifactStore`. In Compose,
the sole `dlr_artifact_store` mount belongs to Control. Workers do not mount that
volume; they use the internal claim-token-protected download endpoint for files
authorized by the current Execution Lease. Browsers and Workers never receive a
storage key, Control path, or deletion credential.

All new Control/Worker deployments use protocol v2 before managed-files
execution is opened. A v1 Worker continues to support `none`/`json` and must be
rejected at the protocol gate for managed-files. Scaling adds Workers, never
additional ArtifactStore writers. GC locks Artifact/Lease first and performs
filesystem I/O after releasing database locks.

Workers declare v2 with `DLR_WORKER_PROTOCOL_VERSION=2` and report deferred
cleanup completion only through canonical
`POST /api/workers/executions/{execution_id}/workspace-cleanup`, using the
independent Cleanup Token. A `deferred` Result accepts only the stable
`workspace_cleanup_failed` reason. Business success remains independent of
Workspace cleanup state.

## Migration and legacy compatibility

Upgrade in the pinned order and record the Alembic head on the target:

```sh
docker compose run --rm control alembic upgrade head
docker compose ps
```

The deterministic backfill maps a manual Task to `json` `{}` and a Schedule's
legacy `input` to the same value; Webhooks do not receive a Task input config.
The backfill is repeatable and fails fast on conflicts while reporting adapter
and source-type counts. The old `adapter_schedules.input` column remains during
the compatibility window and is mirrored in the same write transaction; the
Scheduler reads the new AdapterInputConfig only. Managed Input tables, Blobs,
and deletion jobs are additive migrations.
Alembic `0026` through `0029` `downgrade()` functions are test-only cleanup
paths. Production rollback must not invoke them because they discard input
authority or immutable Execution snapshots.

## Non-destructive rollback drill

Rollback is a deployment rollback, not a database downgrade. First stop new
managed uploads and managed Executions, disable Schedule/Webhook admission, and
drain `pending/running` rows until their Leases are released. Keep history,
Blobs, new tables, deletion jobs, and legacy columns.

```sh
# Use the deployment compose file and protected secret store; placeholders only
export DLR_MANAGED_FILES_ENABLED=false
export DLR_ARTIFACT_DELETE_ALERT_THRESHOLD=5
docker compose up -d --force-recreate control web account-web
curl -fsS -H 'Authorization: Bearer <ADMIN_TOKEN>' \
  http://<control>/api/system/managed-input-capability
# Must show at least managed_files_enabled=false and ready=false
# Managed upload/config writes must return input_source_not_available
```

Do not run `alembic downgrade`, remove the ArtifactStore volume, clear legacy
columns, or delete deletion jobs during rollback. Keep v1/v2 Workers and all
tables so existing `none/json` traffic remains readable. After comparing counts,
history, and Blob presence, reopen the current release:

```sh
export DLR_MANAGED_FILES_ENABLED=true
docker compose up -d --force-recreate control worker web account-web
docker compose ps
```

Verify capability is `true/true`, old configs and history are still readable,
then let GC/Lease governance continue. If active work cannot drain, keep the
flag closed and escalate; never kill a process or release an unknown Lease.

## Retention and audit

`system_default`, custom, and permanent (`manual_delete`) retention are bounded
by the administrator's `max_custom_retention_seconds`, quota, and low-watermark
policy. Expiry moves through GC, which checks Leases before deleting and releases
capacity. Administrators see policy, usage, quota, and `over_quota`; ordinary
users see capability only. `expires_at=NULL` means either `manual_delete` or a
staged lifecycle whose final retention is not fixed; it does not bypass
governance. After repeated deletion failures reach the alert threshold,
automatic retries stop and an administrator must use the retry endpoint above.
Sub-threshold failures keep their bounded backoff and cannot be released early by
the administrator endpoint. Once thresholded, the business delete endpoint returns
`input_artifact_retry_not_allowed` rather than bypassing governance. Capability also
returns ordered `allowed_extensions`; browser accept and prevalidation derive only
from that safe field, while server validation remains authoritative.
Execution history may download business stdout/stderr logs, but Managed Input
history never offers input-file download, reuse, or restore. Logs may contain
safe error codes and counts, never
Bearer/Cookie/CSRF values, storage keys, host absolute paths, or file contents.
