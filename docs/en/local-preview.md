# Fixed-entry local preview

Consecutive pull requests use one environment selected by private configuration. The controller preserves its database, material volumes, administrator token, and master key. Controller source lives in `tools/local-preview/`; `DLR_PREVIEW_HOME` selects the private installation directory. Personal addresses, ports, paths, credentials, and runtime evidence never belong in the repository.

## Select an acceptance target

When a pull request needs local acceptance, select that PR. The controller waits for CI on its current full HEAD and then updates the existing preview.

```sh
python3 "$DLR_PREVIEW_HOME/preview.py" select <PR_NUMBER>
python3 "$DLR_PREVIEW_HOME/preview.py" status
```

`select` accepts only an open, non-draft PR from the configured repository. The latest `ci.yml` pull-request run for the exact HEAD must succeed, including the `backend`, `web`, and `compose-smoke` jobs. A rerun for the same SHA does not redeploy it. Closing or merging the selected PR stops updates and leaves the current application available. The controller never merges a PR.

Status separates the configured PR, current candidate, last verified deployment, current action, and unfinished attention state. `Ready` is controller evidence; it does not replace user acceptance.

```sh
python3 "$DLR_PREVIEW_HOME/preview.py" pause
python3 "$DLR_PREVIEW_HOME/preview.py" resume
python3 "$DLR_PREVIEW_HOME/preview.py" copy-token
```

`pause` lets an in-flight operation finish and then prevents another update. The resident watcher can remain running: wait for the active operation to finish before planning, and `plan-carry-forward` then excludes watcher operations until its manifest is durable. A concurrent `resume` waits for planning to finish. `copy-token` sends the existing administrator token directly to the clipboard without printing it.

## Upgrade rules

The candidate must descend from the deployed Git commit. The current database revision must be an ancestor of the candidate's unique Alembic head, and existing revision identities and parent links must remain unchanged. A private `history_anchor_sha` can reconcile documentation-only history rewrites only when all deployed source paths remain byte-identical. It does not change the recorded runtime SHA or image identity.

Source is extracted at the complete SHA into an unshared VM with no host mount or SSH-agent forwarding. The controller records exact image IDs and rechecks selection, HEAD, CI, compatibility, and the database revision before switching.

The normal switch first requires execution and cleanup responsibility to be idle. It stops Control to close API, scheduler, retry, Relay, DLQ, and GC writers, checks again, and then stops Worker and Web. It creates a PostgreSQL custom-format backup, verifies the backup list, compares persisted asset hashes, applies the one forward migration head, and starts the candidate. Health, exact images, private cgroup isolation, one real RabbitMQ-to-Worker execution, and workspace cleanup must all pass before the deployed state changes.

Backups live below the configured VM root at `backups/<timestamp>-<old>-to-<new>/`. Asset evidence contains column names and row hashes; the database dump contains private data and remains in a restricted directory. A database backup does not replace material-volume preservation. Deployment never removes a volume.

### Explicit carry-forward preservation

The default path still requires all runtime responsibility to be idle. A one-use private carry-forward manifest is available only for explicitly selected responsibility stranded by a known infrastructure Incident when the candidate provides a forward-compatible disposition. It is not an `ignore busy` setting. It never cancels the old Execution, rewrites cleanup, releases Admission, or deletes a volume.

First let the ordinary watcher stage the candidate and stop at the busy gate. Pause the watcher, then create a private `0600` selection file under a `0700` parent:

```json
{
  "queued": [
    {"execution_id": 123, "incident_ids": [456]}
  ],
  "cleanup_execution_ids": [789]
}
```

IDs must be explicit positive integers. Wildcards, duplicates, empty selections, and client-asserted cleanup classifications are rejected. The numbers above are structural placeholders and do not identify any environment.

```sh
python3 "$DLR_PREVIEW_HOME/preview.py" pause
python3 "$DLR_PREVIEW_HOME/preview.py" plan-carry-forward \
  --to-sha <FULL_CANDIDATE_SHA> \
  --ids-file <PRIVATE_IDS_JSON> \
  --output <PRIVATE_MANIFEST_JSON>

python3 "$DLR_PREVIEW_HOME/preview.py" select <PR_NUMBER> \
  --carry-forward <PRIVATE_MANIFEST_JSON>
python3 "$DLR_PREVIEW_HOME/preview.py" resume
```

`plan-carry-forward` accepts only the eligible HEAD of the selected PR. Its images must already be staged by the normal watcher. Planning uses `prepare-sandbox-host.sh --status`; it does not create or repair a keeper. The manifest binds repository, PR, old and new SHA, old and new schema, migration graph, trusted controller files, exact images, every named volume, and the explicit selection. It cannot be rebound to another HEAD or controller. `select` copies it into the controller's private directory. Configuration and `status` retain only its ID, digest, and candidate binding, never the Execution list, private paths, volume names, or journal content. A plain `select` clears an old reference.

The verifier opens a REPEATABLE READ READ ONLY database transaction over a fixed table allowlist: Execution, Attempt, Slot, Incident, Outbox, Adapter and global Admission, input Lease and Hold, credential snapshots, idempotency, schedule outcomes, and Worker cleanup requests. It records old columns, primary keys, row hashes, and counts without publishing raw values. Eligibility requires only the selected queued Executions with their exact open Incidents, unreleased Admission, a current-generation Outbox row, no active Attempt or Slot, no other queued/running/retry_wait Execution, and no active Worker cleanup request.

Cleanup is derived without changing the database:

- `not_applicable`: zero Attempts, `attempt_count=0`, no worker or start fact, and no workspace, journal, or Sandbox evidence. The original `pending` value remains unchanged.
- `completed`: terminal Attempt history and an existing `completed` database fact, with no remaining workspace or journal.
- `deferred_preserved`: any historical terminal Attempt still has a `deferred` cleanup summary and a private cleanup journal whose Execution, Attempt, path, and token digest match the database. This old responsibility still needs a real Worker receipt even when a later Attempt has made the Execution cleanup field completed.

Missing journals, unknown files or symlinks, an unselected workspace, an unknown cgroup, an active Attempt or Slot, an extra open Incident, or material-tree drift blocks the upgrade. The verifier mounts Worker runtime/journal and Control builtin/artifact volumes read-only. Dependency caches are not Execution responsibility, while their named-volume identity is still fixed and preserved.

During the switch, the controller validates the manifest, stops Control, and reads the database again. Only then does it stop Worker and Web, verify that application containers are stopped, keep the same trusted keeper, and require the delegated tree to contain only `agent`. The old database projection, responsibility classification, journals, runtime/material trees, and kernel evidence must match after writers stop, after backup, and after migration before candidate services start. New migration objects are allowed; every old manifest column is still compared. Existing asset, backup-list, image, CI/history, Sandbox, real execution, and workspace-cleanup gates remain in force.

After Control has stopped for a carry-forward switch, a Claim, evidence drift, or any unknown read failure leaves the applications stopped with attention for review and replanning; one old health result never authorizes automatic writes again. Once `migrating` is recorded, the controller likewise does not automatically downgrade, restore, or start an old-schema application. It preserves the site, volumes, and backup for diagnosis. A successful receipt exposes only manifest ID, digest, and count. It proves preservation into the new version; it does not prove that an old Incident was recovered or terminated, or that deferred cleanup completed. Acceptance must follow the original Execution ID, generation, Attempts, output, and resource release. A new successful task is not a substitute.

## Install or update the controller

The installer upgrades an existing private deployment. It requires macOS, Python 3.11+, authenticated `gh`, Colima, the existing LaunchAgent, source cache, private configuration/state/environment, and the prepared Sandbox helper. It does not create a first VM or credentials and does not change the default Docker context.

```sh
python3 tools/local-preview/install.py --start
```

The installer pauses updates and refuses to unload the watcher or replace files while a controller operation or carry-forward plan is active. After it owns the operation and configuration locks, it makes a restricted backup, unloads the LaunchAgent, takes the watcher singleton, installs the reviewed scripts including `carry_forward.py`, transfers trusted VM scripts over stdin, records file SHA-256 values, restores configuration, and optionally restarts the watcher. It refuses an unfinished deployment.

Private `config.json` supplies repository/PR polling, Colima profile, Compose project, VM root, local port, LaunchAgent label, and Sandbox unit/resource envelope. There are no personal defaults. `preview.env`, build proxy settings, credentials, manifests, backups, and raw receipts remain private.

## Offline recovery and attention

If the VM or application is stopped and the transaction is complete, the controller restores the last successful SHA from exact local images without rebuilding or migrating. If containers are still running but health fails, it preserves them for inspection.

Build failure leaves the old application running. Once a switch begins, the controller writes durable attention. Any interruption, migration failure, or verification failure blocks automatic retries. Inspect `status`, private `deploy.log`, and VM `transaction.json`, then reconcile the actual schema, image IDs, current SHA, receipt, and backup. Database restore requires an explicit operator decision with writers stopped; the controller never performs an automatic downgrade.

Only after the VM transaction is again `ready` at the last successful SHA may an operator run:

```sh
python3 "$DLR_PREVIEW_HOME/preview.py" acknowledge
```

Do not delete attention as a substitute for reconciliation.

## Development verification

```sh
python3 -m unittest discover -s tools/local-preview/tests -v
python3 -m py_compile tools/local-preview/*.py tools/local-preview/tests/*.py
bash -n tools/local-preview/deploy.sh
openspec validate issue161-runtime-reliability --type change --strict --no-interactive
git diff --check
```

Controller tests are independent of the application backend. Real PostgreSQL migration, RabbitMQ responsibility, keeper/volume inspection, and user-visible recovery remain separate integration-owner gates.
