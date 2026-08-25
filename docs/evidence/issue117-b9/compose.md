# Issue #117 Batch 9 Compose evidence

- Dispatch: `issue117-b9-logdocs-20260825-r1`
- Baseline: `3070959dbcaa7a777da4066fe8e24bce4420023c`
- Scope: isolated local documentation/Compose verification only; no business browser flow.

## Preparation and config

An isolated temporary host root was prepared with exactly these directories:

```text
control/
worker/
web/
account-web/
postgres/
```

`docker compose config --quiet` passed with the isolated root and anonymous
placeholder environment values.

## Smoke

The first scoped smoke attempt used `/private/tmp`; Colima did not expose that
path to the Docker bind-mount layer, so PostgreSQL exited before health became
ready. No containers remained after its scoped cleanup.

The smoke was rerun with the repository's ignored, repository-local temporary
root (the existing script's Docker Desktop/Colima-compatible path):

```text
./scripts/compose-smoke.sh: PASS
PostgreSQL init/health regression: PASS
M5.4.4 compose smoke: PASS
service-log secret redaction checks: PASS
scoped project/container/volume/network cleanup: PASS
```

No global Docker cleanup was run. The temporary root and the unique Compose
project were removed; a scoped `docker ps -a --filter name=dlr-i117-b9` check
returned no rows.
