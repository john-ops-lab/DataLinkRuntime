# Issue #127 B4 evidence

This directory contains sanitized, repository-relative evidence for the
`issue127-unified-input-object` B4 gate. It contains no request/response
headers or bodies, credentials, tokens, host paths, or raw service logs.

## Provenance and scope

- `DISPATCH_ID`: `issue127-b4-gate-20260827-r1`
- `APPROVED_CHANGE`: `openspec/changes/issue127-unified-input-object`
- `FIXED_BASE`: `1d91452fdb4dc2b6b302555d3ff7983940a31137`
- Scope: only `tasks.md` 9.1–9.5. No 10.x task was implemented or checked.
- Runtime: one isolated Compose project for the lifecycle run, with
  `ao.session=datalinkruntime-135` on all worker-created containers.
- Credentials: only anonymous test placeholders were used; no credential
  material is retained here.

## 9.1 red → green

The first config assertion was intentionally run before the Compose edit. It
failed with three expected assertions: the Control ArtifactStore volume,
explicit managed-files flag, and explicit GC interval were absent. The
post-edit assertions are green:

- default `docker compose config -q`: `PASS`;
- DNS override `docker compose config -q`: `PASS`;
- default flag: `false`;
- default container path: `/var/lib/dlr/artifacts`;
- override root, flag, and intervals rendered as expected;
- the named `dlr_artifact_store` volume is mounted by and writable through
  `control` only; worker/web/account-web have no ArtifactStore mount.

See `compose-config.json` for the sanitized values.

## 9.2 lifecycle and capacity

The isolated lifecycle run completed `68` assertions with `PASS`. It covered
allowed, blocked-extension, actual oversized, refresh/recovery, 0 and 8
selection, replacement, expiry, idempotent delete, quota lower/recover, and
low-watermark lower/recover flows. The run ended with zero reserved capacity,
zero partial uploads, zero quarantine entries, and all deletion charges
released exactly once. See `lifecycle.json`.

## 9.3 fault injection and restart convergence

The run injected interrupted upload recovery, rename-then-DB failure,
reservation TTL race, GC crash after a committed claim, store delete failure,
and Adapter delete competition. Compensation/retry assertions were green.
After those faults, Control was stopped and restarted with the managed-input
flag on, health/database checks were green, then stopped and restarted again
with the flag off. See `fault-injection.json`.

## 9.4 rollback

Rollback used only the owned Compose Control service stop/start path. It did
not run schema downgrade, drop tables, delete historical objects, or remove
the named ArtifactStore volume. The post-rollback database/blob/job snapshot
matches the pre-rollback counts, and a managed-input request with the flag off
returns the expected unavailable-input response. See `rollback.json`.

## 9.5 Wave B gate

- Fresh migration and the fixed-base migration tests passed; the explicitly
  destructive test-only downgrade was not run and is recorded as deselected.
- Repeat `alembic upgrade head` was run twice against the isolated database;
  the catalog remained at `0028_issue127_b0_managed_input`.
- Backend quality gates passed: Ruff, format check, mypy, compileall, the
  related Issue #127 suite, and the full backend suite with only the prohibited
  downgrade test deselected.
- `scripts/compose-smoke.sh` completed with `SMOKE_RC=0`, including default
  and DNS Compose config, fresh head, service health, authentication boundary,
  fake Provider/IMA regression, and sensitive-value log assertions.
- Sanitized repository/evidence scans are recorded in `scans.json`.

## Environment failure chain

The initial Docker socket access was denied by the sandbox and passed after a
narrow retry for the same local Colima command. A first backend invocation from
the repository root failed because Alembic requires the `backend` working
directory; the same relevant command passed from `backend`. The first smoke
attempts failed because the PostgreSQL bind-mounted disposable log directory
was not writable for the container user. The fix was limited to the owned
temporary directory, after which the unchanged smoke script passed. One
restart probe also lacked required anonymous Compose environment variables;
the retry supplied placeholders and passed. These are environment/setup
failures, not product failures, and no raw host paths or credentials are
archived.

## Resource cleanup

The final resource manifest records exact Compose project ownership, labels,
and cleanup status. No worker-created application or temporary Compose
project is intentionally left running at handoff.
