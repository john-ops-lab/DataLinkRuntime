# Fixed Worker deployment and maintenance

Use mode A for one Control and a fixed Worker on the same host. Use mode B for a Control and one or two fixed Workers on separate hosts. Prepare each Linux Worker with the existing sandbox host script and persistent journal anchor. The standalone Worker needs outbound HTTPS and AMQPS, an identity-bound runtime credential, a private CA and trust configuration, its own fixed-queue account, and its original local volumes. Control creates the Broker topology. PostgreSQL, Broker and Control remain single points of failure in these deployments.

## Upgrade and authority

Keep the existing Compose project, Worker ID, volume and journal anchor when upgrading from the J image. Apply the supported stopped-writer transition from schema `0089_issue129_credential_ops` to `0090_issue129_worker_cutover`. Check the entire source image receipt chain, back up the original database and data, then use the image transition's separate `observe` operation to confirm the commit.

Mode A can grant previously registered protocol 3 fixed Workers a single transition window, at most seven days on the database clock. New installations start with it closed. Use the owner-only `legacy-window-open`, `legacy-window-status` and `legacy-window-close` commands in `python -m dlr.control.control_identity_init_v4`. After the original Worker has stopped and its operations and queue have settled, adopt the existing numeric ID, UUID, name and Adapter bindings with the v4 identity adoption API. The request includes the SHA-256 of the operator's private stop evidence. Enrollment and explicit activation follow adoption. Expiry and explicit closure leave outstanding records available for reconciliation.

Before rotating the Control proof key or changing the database primary, freeze intake and stop the old writers. Use `proof-authority-apply` and `proof-authority-observe` with matched source and target declarations and keys. Retain the confirmed cluster, epoch, operation and image chain outside the database so that a restored older database can be detected. Reinstall the matching Worker trust files before restarting. Keep historical image receipts and undelivered work with the original identity.

## Runtime options

| Setting | Supported fixed Worker configuration |
| --- | --- |
| `DLR_WORKER_LANGUAGE_MODE` | `python`, `python_javascript`, `python_javascript_typescript_java` or `python_javascript_typescript_java_go`. The image carries the fixed tools, and the Worker publishes capability after native sandbox checks. Compiled steps use trusted dependencies already in the image. |
| `DLR_WORKER_INPUT_MODE` | `disabled` or `managed_files`. A file is transferred under the current identity, instance and Attempt proofs, and must match the frozen reference. |
| `DLR_WORKER_BUILTIN_WHEEL_MODE` | `pure_python` requires managed-files mode and a separate sandbox probe. One frozen builtin PyPI wheel with an exact `name==version` requirement runs in the Attempt's own staging scope. |
| `DLR_WORKER_CACHE_ROOT` | Optional absolute path to a separately mounted, Worker-owned local cache. Keep mode 0700 and preserve its existing v4 owner record. Admin preview and clean commands select at most eight observed keys. |

Keep exactly one queue account per Worker. The credential can consume its own queue. Use a separate deployment account for topology and publishing. Pause new submissions to the target Worker and leave it active until queued work settles. Then drain it, verify that ready and unacknowledged deliveries are zero, and stop the Worker. Install the verified new Broker configuration atomically before revoking the old account. Verify the old connections are gone and the new account can access only its queue. After the original lease expires, start the Worker in the disabled state and activate it explicitly. Retain the runtime HTTP credential, CA, stable identity, and original storage during this operation.

## Maintenance and transport

Drain closes new scheduling while the original Attempts finish. Force-stop records the exact original Attempt and fence in the current maintenance cycle, disables scheduling, and denies successor execution proofs. Treat force-stop as accepted until the physical process, cgroup, IO and local holds have been observed. Recovery requires the original same-boot storage, a verified stop checkpoint, and a current Control-authorized observation. An explicit no-result disposition retains the original outcome as unknown.

Deleting an Adapter with v4 history first freezes it. Control retains the Attempt and its receipts until the assigned Worker confirms local cleanup under the same stable identity and a current instance proof. A Worker with a dedicated cache also claims an object-level guard before retiring that Adapter's entries. A deployment with unverified storage keeps its cleanup responsibility.

Validate TLS certificate chains and hostnames for both HTTPS and AMQPS. When a reverse proxy is used, verify the proxy's upstream TLS connection to Control as well as the Worker-facing connection. The standalone Control's TLS listener disables forwarded-protocol rewriting. Monitor the PostgreSQL clock and each Worker's elapsed clock against the deployment's lease drift assumption. When a clock jump, suspension or missing observation makes the bound uncertain, retain the work and reconcile before resuming.

Use the existing host monitoring to track clock synchronization, offset, and observation freshness for PostgreSQL and each Worker. On hosts using `systemd-timesyncd`, check `timedatectl show -p NTPSynchronized -p NTP`, record offset and the poll interval from `timedatectl timesync-status`, and monitor synchronization changes. Investigate Worker clock observations or heartbeat exchange failures together with the current lease. Pause new submissions on a clock jump, loss of synchronization, or missing monitoring. Reconcile existing responsibilities and explicitly activate after restoring trusted clock observations.

Execution history for live Adapters follows the regular time and count retention policies once the current Worker confirms its original journal has been retired. Control binds the confirmation to the settled Attempt and cleanup receipt. History with active holds or missing retirement evidence remains available for controlled reconciliation or Adapter cleanup.

For exact commands and the supervised recovery procedure, see the [Chinese deployment runbook](../zh-CN/remote-worker-wave1a.md).
