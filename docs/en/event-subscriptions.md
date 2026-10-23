# Event subscriptions: management and durable custody

The feature defaults to `DLR_EVENT_SUBSCRIPTIONS_ENABLED=false`. In this branch's B5 candidate, enabling the flag makes `GET /api/event-subscriptions/capabilities` advertise `kafka` and `kubernetes`. Kafka passed B3; the Kubernetes backend passed B4, while its B5 product/browser and remaining real-target gates are still pending. Candidate enablement does not establish final product delivery. The implemented diagnostic registry and public enablement registry currently contain the same two sources; diagnostics remain a separate operation. A successful connection test does not establish business success.

## Management and authorization

One non-deleted subscription binds one Task/manual Adapter. Reads use its view/read permission, mutations require edit, and selecting source credentials follows existing owner/admin rules. Lists filter authorization before counting and pagination. Configuration contains credential references; passwords, tokens and private keys are not accepted as source configuration.

All routes use `/api/event-subscriptions`:

| Method and suffix | Behavior |
| --- | --- |
| GET `/capabilities` | Actual flag and available sources |
| GET `/runtime-status` | Admin-only listener count and application buffer usage/peak/limit in the current Control process |
| GET/POST root | Authorized listing or a new disabled subscription |
| GET/PATCH/DELETE `/{id}` | Detail, configuration/metadata update, or deletion after stopping and draining |
| POST `/{id}/enable`, `/{id}/disable` | Start/stop reception; accepted custody continues draining |
| POST `/{id}/test-connection` | Bounded diagnostic for an implemented source, requiring the feature flag, edit permission and expected version; no business execution |
| POST `/{id}/clone` | Disabled copy bound to another editable Adapter, without progress |
| POST `/{id}/reset` | Audited request ID/reason and a new epoch after stopping/draining |
| POST `/{id}/repair/enter`, `/repair/revisions`, `/repair/exit` | Restricted code repair with backlog |
| GET `/{id}/events`, `/{id}/events/{receipt_id}` | Event and original/replay execution chain |
| GET `/{id}/checkpoints` | Read-only, bounded current-epoch safe/confirmed positions, barriers and unaccepted faults |
| GET `/{id}/activations/{activation_id}` | Activation version, Worker and credential-reference metadata, without secret decryption |
| GET `/{id}/events/{receipt_id}/replay-context` | Read-only original/current reference comparison and concurrency fingerprint |
| POST `/{id}/events/{receipt_id}/replay`, `/skip` | Idempotent replay or an explicit unprocessed disposition |
| POST `/{id}/skip-unaccepted` | Explicitly skip a matching unaccepted Kafka fault; the server derives the safe next position |
| GET `/{id}/quota-audit`, POST `/{id}/quota-repair` | Read-only accounting audit or explicit repair after disabling and confirming listener closure |

Runtime-affecting operations include `expected_config_version`; stale versions return `subscription_config_conflict`. Replay/skip/reset also require `request_id` and a nonempty `reason`, and request IDs cannot be reused with different semantics. Name/description updates are independent. Lists use bounded `before_id` pagination.

Inspect desired, connection and delivery states separately. `event_counts.accepted` counts currently retained Receipts and `replayed` counts retained subsequent Deliveries; neither is a lifetime counter. `limits` contains resolved subscription limits, while `global_limits` describes shared ceilings; reserved control capacity is separate. Checkpoint `source_lag=null` means unknown: the safe/confirmed position difference is not broker message lag. Diagnostic reads perform no source RPC and create neither leases nor quota records.

The event's `activation_id` identifies its original activation. Each Delivery's `effective_activation_id` identifies the original or explicitly overridden current-configuration activation, and `revision_override_id` identifies a restricted repair Revision. Compare the selected Delivery rather than treating the original activation as the configuration for every replay. Credential usage includes retained source configurations and activation references, exposing only identifiers and binding fields, never secret values.

Current-configuration replay requires a disabled subscription with no other unfinished custody. Before confirmation, read `replay-context` to compare Revision, fixed Worker, configuration version/epoch and credential references. Submit `use_current_configuration=true` with that preview's `expected_preview_token`. Under the same Adapter lock, the server rechecks all references; a change returns `subscription_config_conflict` rather than executing an undisplayed configuration. The fingerprint checks concurrency, not authorization. After an unknown submission result, retain the version, request ID and fingerprint when retrying. Before refreshing the preview or changing the selection, reconcile the previous request's result and begin a new request. An unavailable fixed Worker never silently becomes automatic selection.

Normal manual tests retain the saved Task InputConfig. Backend guards block runtime mutations and ordinary triggers while a subscription receives, stops, drains or repairs. A bound subscription must be explicitly deleted after stopping/draining before changing the Task trigger mode or deleting its Adapter.

### Kubernetes batch APIs and B5 candidate status

These backend interfaces passed B4. The B5 candidate permits Kubernetes enablement through the ordinary authorized API when the feature flag is enabled; no test-only public bypass is required. New subscriptions still start disabled. The Web must use capabilities to expose supported sources; its B5 workflow and final delivery status remain subject to separate acceptance. Saving a configuration or passing diagnostics does not start reception.

| Method and suffix | Behavior |
| --- | --- |
| GET `/{id}/sync-batches` | Authorized retained batches for this subscription, with `before_id`, `limit=1..100` and optional `source_epoch` filtering |
| GET `/{id}/sync-batches/{batch_id}` | Frozen activation/epoch, read/completion facts, pending controls/reserve, gaps and at most three control-custody summaries |
| POST `/{id}/sync-batches/{batch_id}/abandon-completion` | Dedicated completion abandonment with an unconfirmed business result; ordinary skip cannot substitute for it |

The subscription's `latest_batch` and batch detail distinguish `read_complete`, `completion_committed`, `completion_abandoned`, `closed`, unmaterialized controls and remaining reserve. An empty batch may still carry obligations; neither zero Receipts nor `state=failed` establishes draining completion. RVs remain opaque strings. Responses exclude raw pages, continue tokens, server addresses, CA contents and Secrets. Read-only batch queries make no source calls and remain available with the feature disabled.

Abandonment requires edit permission, `expected_config_version`, `source_epoch`, the completion `receipt_id`, `expected_latest_delivery_id`, `expected_delivery_ordinal`, an explicitly supplied nullable `expected_execution_id`, the original `request_id`, a nonempty `reason` and `confirm_impact=true`. Reception must be disabled, native closure confirmed and all associated Executions terminal. A latest pending replay without an Execution may be atomically abandoned. An Execution or new replay that changes the expected association wins a conflict; this action cannot cancel running executions.

The action preserves original failures, input and the committed completion fact, using one bounded audit within existing capacity. It releases only the completion barrier: other failures still require repair, old WATCH custody drains in order, and neither enablement nor a new LIST starts automatically. It cannot undo target effects, and the old completed chain can never gain new execution permission. After an unknown result, retry with all original parameters and the same request ID. The same request can still read its original result under current authorization after a later legitimate configuration update, without settling twice. Disabling the feature does not prevent settlement of existing custody.

A successfully confirmed, closed completed control also rejects new original/repair/current-configuration replay. Under current authorization and matching complete original parameters, its original request reads back the existing Delivery before checking the version or eligibility of a new action; later legitimate configuration updates or feature disablement do not block this readback. It creates no new Delivery, Execution, Outbox or audit. A new full reconciliation uses a new complete batch. Ordinary object/WATCH replay rules remain unchanged.

Kubernetes configuration freezes logical cluster, HTTPS origin, effective CA trust and GVR/namespace/selectors identity; server URLs cannot include API paths. Trust or scope changes require stopping, settlement and an explicit `new_epoch`, rather than silently reusing an old RV. Rotating a token value under the same credential reference retains the epoch. A legacy configuration without a verified identity snapshot is not silently backfilled: create an immutable configuration in an explicit new epoch.

## Durable delivery and recovery

T1 atomically commits the complete Envelope, custody, quotas and safe source position before source acknowledgment. T2 atomically transfers pending custody to Admission, Execution, Outbox and the delivery link. Queue unavailability leaves custody pending. Database-time leases and generation fencing reject stale owners; listener permits remain occupied until the native connection actually closes.

Execution uses `trigger=subscription` and the existing JSON protocol, with public snapshot `{source_type: json, revision: null}`. Event input never overwrites the saved Task input. Original activation Revision, Worker and credential references remain frozen; secret values are not snapshots. Subscription replay uses its own authorized action, not ordinary Execution replay.

Repair stops reception and new dispatch, waits for nonterminal executions to settle, and creates an immutable code/dependency-only repair Revision. An explicitly selected replay may use it for the failed event. The original activation and later pending events retain their original version.

Pending, retained payloads, deduplication metadata and receipt rates have independent limits and low-water recovery. Default payload/dedupe windows are 7/30 days; unfinished custody, failure barriers and active batch references remain protected. Expired payload replay returns `event_payload_expired`. Logical quotas do not measure physical database/log storage.

Background accounting audits use bounded batches and never automatically change custody or counters. After disabling reception and confirming actual listener closure, an editor may submit `quota-repair` with the expected version, request ID and reason. It recomputes this subscription's ledger from durable responsibilities and rebuilds the shared aggregate. Pending custody may remain so a corrupt counter cannot make draining and repair mutually impossible. Events and Executions remain unchanged.

## Kafka configuration and connection diagnostics

The client is pinned to `kafka-python==3.0.11`. Isolated probes use Apache Kafka 4.1.2 with single-node KRaft; they do not establish acceptance for arbitrary broker versions, multi-node availability or every fault combination.

| Configuration | Rule |
| --- | --- |
| `cluster_id` | The stable ID actually reported by the broker; it must match on connection |
| `bootstrap_servers` | 1–16 explicit `host:port` entries, without credentials or URL paths |
| `topics` | 1–16 exact names, without regex; at most 16 partitions accumulated per epoch |
| `group_id` | Dedicated to this subscription; omission and cloning generate independent groups without copied progress |
| `initial_position` | Explicit `earliest` or `latest`, resolved only when a partition first receives its checkpoint in that epoch |
| `security_protocol` | `SSL` by default, `SASL_SSL`, or `PLAINTEXT` used by isolated probes |
| `sasl_mechanism` | `SCRAM-SHA-256` or `SCRAM-SHA-512` with `SASL_SSL`; rejected with other protocols |
| `credential_id` | Secret Store reference; SCRAM uses `username` and `password` fields, never values in configuration |
| `ca_pem` | Optional public CA PEM; otherwise system trust applies. Certificate/hostname verification stays enabled; no private keys |
| `mode`, `ordering` | Fixed to `per_event`, `partition_serial` |
| `limits` | May tighten known quotas only; cannot relax global limits |

Diagnostics require cluster/topic metadata and group-query permissions. Reception additionally needs consumption and offset-commit permissions for the selected topics and dedicated group. Foreign members or another subscription occupying the same source/group are rejected. An idle group does not authorize its existing committed offsets to override DLR custody.

With the flag enabled, connection testing inspects implemented Kafka. Supply `expected_config_version`; the response contains verified `source_type`, `cluster_id`, topic/partition counts and group availability. Testing does not join a group, commit offsets, write checkpoints or execute an Adapter. SQL preparation, decryption and transaction release run in the synchronous API pool; source RPCs use the dedicated budget. Responses exclude source addresses and secret values.

B2 isolated probes cover TLS with both SCRAM mechanisms, wrong passwords, untrusted CAs, and credential rotation retaining the same epoch/checkpoint. A real committed/aborted transaction probe checks `read_committed` visibility and legitimate offset holes. These results do not replace B3 business and capacity acceptance. SASL_PLAINTEXT, OAuth, GSSAPI, client-certificate authentication and other broker/platform combinations are outside this batch's verified scope.

### Diagnostic codes and the pinned client boundary

| Code | Operational meaning |
| --- | --- |
| `source_credential_invalid` / `source_credential_unavailable` | Check mechanism, Secret Store fields, reference and decryption availability |
| `source_authentication_failed` / `source_authorization_failed` | A definite authentication/authorization error was observed; `blocked` requires explicit resume after correction |
| `source_tls_failed` | An observed TLS/certificate error; correct trust/hostname configuration rather than disabling verification |
| `source_temporarily_unavailable` / `source_connection_timeout` | Network or outer waiting did not complete; timeout alone does not prove a wrong password |
| `source_protocol_unsupported` | Client/protocol or tested internal interfaces do not satisfy the contract; reception is blocked |
| `source_identity_unavailable` / `source_identity_changed` | A nonzero topic UUID is unavailable, or cluster/topic identity changed; review the source and whether a new epoch is required |
| `source_checkpoint_ahead` / `source_history_gap` | Broker commits lead custody, or the saved position is outside the readable range; preserve responsibilities and review recovery explicitly |
| `source_close_unconfirmed` | Native close is unconfirmed; retain the owner/permit rather than opening a replacement |

In 3.0.11 an outer bootstrap timeout may hide the underlying authentication/TLS error. The connector's tested internal `KafkaNetClient` / `ObservedConnectionManager` adapter observes typed failures; callbacks retain only bounded machine codes. Missing/incompatible interfaces block with `source_protocol_unsupported`. This internal dependency is pinned with the exact client version: upgrades must revalidate error observation and resource closure. Without a definite observed typed failure, an outer timeout remains temporary unavailability, not an assumed authentication failure. Raw SDK debug logs do not enter ordinary platform logs.

## Kafka positions, stopping and recovery

Only after T1 commits complete custody may the connector commit the record's safe next position, `record.offset + 1`. Auto commit is disabled and `auto_offset_reset=none`. `consumer.position()` is not a safe acknowledgment frontier: it may cross unproven custody, transaction control records or aborted records. Legitimate holes follow records actually read, without inventing missing messages. Kafka commit proves durable DLR custody; T2, Execution success and an actual target write are separate later facts.

Initial earliest/latest positions are persisted before consumption; restarts and rebalances retain existing checkpoints rather than reinterpreting latest. A new partition resolves the same initial-position policy once and persists it. Lagging broker commits may be reconfirmed. Leading commits cannot advance DLR custody without explicit new-epoch authorization. One unfinished business Delivery per partition blocks later dispatch through `retry_wait`, `dead_letter`, `cancelled` and `expired`; other partitions may compete for the existing Adapter Slot.

Metadata must expose the matching cluster ID and nonzero topic UUID. Missing identity does not silently fall back to topic-name deduplication; a changed UUID blocks same-name recreation. UUID metadata is refreshed before accepting a nonempty poll. However, the pinned SDK uses Fetch up to v12 without a request-level topic UUID fence, so detection of every deletion/recreation race is not guaranteed.

The checkpoint conservatively retains the actual record's `offset + 1`; trailing control/aborted markers do not advance it to the client position. Retention may remove those positions and produce `source_history_gap` even without proven loss of business records. Review source retention, the previous checkpoint and target business facts before recovery; this code is neither proof of definite business loss nor permission for silent reset.

Recovery procedure:

1. Inspect desired, connection and delivery states separately, including errors, source identity, pending custody and failure barriers.
2. Disable first fences the generation, rejects stale reception and waits for actual native closure. Accepted custody keeps draining. Cancellation of an HTTP wait, lease expiry or a feature flag does not prove resource reclamation.
3. Automatic reconnect, or explicit resume after correcting a blocked subscription whose desired state remains enabled, retains the epoch, checkpoint and activation. Each new connection reads the referenced credential's current value; rotation never copies secrets into history. A disabled subscription must drain before enable creates a new activation. Changing the credential reference or runtime configuration also requires stopping/draining and saving before enabling again; old custody retains its original activation.
4. For a history gap, scope/initial-position change or source switch, settle or explicitly dispose of old custody. Submit reset with the expected version, request ID and reason, choosing earliest/latest and a new epoch. Old custody, checkpoints and audit remain. A new epoch may reread or skip source history; target business idempotency remains necessary.
5. For an unknown target outcome, replay with the same business ID and independently read the target; see the [ordinary Python example](../../samples/event-subscriptions/README.md). Editing broker commits does not establish business success.

For a fault rejected before Inbox custody, such as an oversized record, checkpoint `blocked_unaccepted` exposes a bounded diagnosis and source-event identity. After review, submit `shard`, `source_event_id`, expected version, request ID and reason to `skip-unaccepted`. The server accepts only a matching current-epoch fault and derives the next position; clients cannot supply arbitrary offsets. Its `skipped_without_processing` disposition creates no Receipt or Execution and implies no target success. The resumed source first acknowledges this durable safe frontier, so skipping the last poison record does not require a later normal record to commit it. Retrying a request must retain the same actor and semantics.

### Listener budgets and closure

Control shares at most 16 source-owner/connection-test permits, without a waiting queue, and one actual owner per subscription. SDK I/O threads and connections are counted separately. Source waiting occupies neither ordinary Worker Slots, the default API pool nor its event loop. Application buffer accounting is shared at 64 MiB, with 2 MiB per owner; raw records and final Envelopes also obey their own and Execution JSON size limits. Native fetch/frame limits do not impose a hard ceiling on decompression or process RSS; B3 must measure capacity.

Database leases last 30 seconds and renew independently every 10 seconds. Temporary connection errors use jittered exponential delays of 1–60 seconds; capacity retries use 1–30 seconds and require low-water recovery. Definite authentication, authorization and identity failures await explicit action. Connection-test HTTP waiting defaults to 15 seconds. Timeout/cancellation ends that wait only; the permit remains occupied until native work actually returns and closes. Failed closure retains the permit; repeated enable cannot bypass it.

Consumer close disables automatic commit. Explicit commit/Consumer close use a 3-second client budget; normal Consumer requests use 10 seconds. Three seconds is not an absolute bound on the entire stop operation or arbitrary OS/NSS behavior, and excludes DNS resolution and sequential RPCs. Resolver/platform behavior must remain within the B0 verified boundary. Turning reception off preserves draining under the current Control; reverting old binaries or removing tables must not erase backlog.

## Kubernetes configuration, batches and recovery

The candidate pins `kubernetes==35.0.0` and `urllib3==2.8.0`. It supports namespaced Pod (`core/v1`) and Deployment (`apps/v1`) resources, with bearer-token references and verified TLS. Other resources, arbitrary API paths and authentication mechanisms are outside this support matrix. `cluster_id` is an explicitly configured logical identity, bound to connection trust and scope; it is not a discovered system-namespace UID.

| Configuration | Rule |
| --- | --- |
| `cluster_id`, `server` | Logical cluster ID and HTTPS origin only; no credentials, API path, query or fragment |
| `group`, `version`, `resource` | Exactly `""`, `v1`, `pods`, or `apps`, `v1`, `deployments` |
| `namespace` | Exact namespace, or explicit `null` for all namespaces allowed by the credential |
| `label_selector`, `field_selector` | Exact selectors for both LIST and WATCH; they participate in scope identity |
| `credential_id` | Secret Store reference with a `token` field; no token value in configuration |
| `ca_pem` | Optional public CA PEM; otherwise the SDK's actual certifi CA bundle is used. Effective certificate DER trust is frozen, and the same verified bytes are pinned for the connection |
| `mode` | `per_event` by default; `state_sync` must be selected explicitly |
| `limits` | May tighten existing quotas only; does not bypass control reserves or shared ceilings |

The credential needs LIST and WATCH permission on the selected resource/scope. Connection testing verifies bounded LIST and WATCH access and returns logical identity, scope fingerprint and trust/list/watch verification facts. It creates no Batch, Receipt, checkpoint or business Execution. It does not prove a complete source snapshot or target reconciliation. Credential truth rotation under the same reference is picked up on reconnection; changing effective CA trust, origin, selectors, resource scope or mode requires disabled/closed reception, settled custody and an explicit new epoch.

`per_event` retains individually accepted changes. `state_sync` can merge pending WATCH state for the same scope/object UID only when that responsibility has never had an Execution; even an older Execution followed by a pending replay prevents merging. LIST members and `DELETED` events are not merged. Existing execution input is immutable. First/current event identity, coalesced count and superseding links preserve the audit trail; coalescing is not business success. Neither mode reconstructs history that Kubernetes has already discarded. A `DELETED` event under selectors can mean leaving the selected scope, rather than physical object deletion; removal must remain UID- and scope-bound.

LIST pages hold at most 16 objects and 1 MiB of raw bytes; WATCH lines hold at most 512 KiB. Successful and error responses are read with raw `read1(4096, decode_content=False)` before JSON decoding, with identity encoding and redirects disabled. Object Envelopes still obey the smaller of 1 MiB and the configured Execution input limit. A batch accepts at most 5,000 objects and 128 MiB of accepted content. An oversized or incomplete snapshot loses cleanup qualification, retains already accepted responsibility and uses its failed-control path; it never proves that unread objects are absent.

Each batch prepays three control slots of up to 8 KiB across pending, history and dedupe accounting at both subscription and global scope. Ordinary objects/WATCH cannot spend that reserve. Ready controls still use the same subscription/global 10/50-per-second rate windows, and wait fairly; reserved capacity does not exempt them from rate or Admission. An empty, unfinished batch or remaining control reserve still locks runtime changes. The sixteen native permits and 64 MiB application-buffer budget are shared with Kafka and diagnostics; permits return only after confirmed native close. Raw/application limits are not a full process RSS bound. The 3-second connect, 2-second socket-read and 5-second between-read deadline do not establish hard DNS/OS cancellation.

The normal path is `sync.started` → LIST members → `sync.completed`: all pages and latest member responsibilities must succeed before completion receives execution permission, and the Adapter must verify its own target writes before cleanup. A typed LIST member may omit `kind`/`apiVersion` after its enclosing PodList/DeploymentList is verified; the original payload remains unchanged. Explicit wrong/null/empty values, standalone WATCH objects and top-level LIST identities remain strict. A failed member can be repaired while valid snapshot evidence remains. Skipping a member or started control permanently invalidates that batch's cleanup qualification.

A partial LIST, permission failure or pre-completion history gap produces `sync.failed`. Successful handling or an explicit audited skip of that failed control opens draining for old objects/WATCH; it does not authorize cleanup or hide other business failures. Completion qualification freezes in T2. A later gap records next-round recovery rather than rewriting the frozen batch as failed. After successful closed completion, new completed replay is rejected; use a new complete batch. Unconfirmed failed completion can use restricted replay/repair or the dedicated abandonment described above.

Disable reception and wait for actual closure; accepted responsibility and prepaid controls continue bounded maintenance even when the feature is off. Re-enable on the same epoch can resume its durable opaque RV with a new activation, without a new LIST when the existing batch remains valid. BOOKMARK only advances after earlier custody commits and creates no business Execution. A `410 Gone` means lost readable history: record the gap and settle the old lifecycle before a fresh snapshot. It does not reconstruct missed changes. A normally enabled recovery may start a new valid batch; abandonment stays disabled and requires explicit enablement after old custody settles. Reset creates a new epoch only after stopping and settlement, preserving history; Kubernetes has no Kafka earliest/latest or arbitrary-offset skip operation.

| Kubernetes diagnostic code | Action |
| --- | --- |
| `source_permission_denied` | Review the referenced bearer token and LIST/WATCH RBAC for this exact scope |
| `source_trust_invalid`, `source_tls_invalid`, `source_identity_changed` | Review effective CA/hostname/trust identity; do not disable TLS verification or silently reuse the old RV |
| `source_history_gap`, `source_list_snapshot_changed` | Preserve accepted custody and inspect the batch's failed/frozen recovery path |
| `source_response_too_large`, `source_content_invalid`, `source_object_invalid` | Review source content or scope; unaccepted data has not completed business processing |
| `subscription_sync_history_capacity`, `subscription_sync_batch_capacity` | Reserve or batch capacity is unavailable; inspect existing custody/retention without relaxing ceilings or deleting protected history |
| `subscription_sync_completion_conflict` | Completion eligibility or expected responsibility changed, or the old control is already settled; reconcile the original request result before a new action |

### B4 evidence and remaining B5 gates

Source `326dca6` passed the B4 backend gate with a real Kubernetes 1.35 API, ordinary Python Adapter/Worker, RabbitMQ Attempt/Outbox links and independent PostgreSQL target readback. The metadata-only fixture had 17 selected Pods, returned as 16+1 pages. LIST members plus started/completed made 19 receipts; one WATCH update made 20. Every target payload/hash and UID matched; the updated object had two effective writes, the other sixteen one each. After stopping, an annotation changed while reception was closed; re-enabling on the same epoch created a new activation and accepted WATCH from the durable RV, with one retained batch, no new LIST, 21 total receipts and three writes for that object. Listeners and application buffers returned to zero.

The initial TypeMeta rejection remains a recorded first failure, followed by a narrow correction that preserves omitted member fields. Its failed control succeeded, the failed batch closed with reserve zero, and reception stopped. Deliberately requesting an old RV established a native `410` diagnostic only; it is not proof of natural platform expiry/recovery. Deployment real-target paths, Kubernetes browser workflows and the remaining B5 representative recovery cases are pending. No Kubernetes sustained-throughput or long-duration capacity result is claimed. Details and unverified scope are in the [B4 review](../../openspec/changes/add-event-subscriptions/b4-review.md); exact-HEAD remote CI and user acceptance remain separate gates.

## B3 validation status and capacity limits

B3 passed independent acceptance; Kubernetes continues through B4/B5. The matching `10ec0f4` deployment completed real Chrome creation, connection diagnostics without joining a group, enable/disable with actual closure, Kafka-to-target processing, restricted repair with same-partition backlog, skip, current-configuration replay and reset. Independent reads preserved original failure outcomes: four business rows each had one effective write, while the skipped event had no target row. Reset retained old Receipts, Deliveries and checkpoints, left Saved Input unchanged and did not re-enable reception. An account with read access to one Adapter saw only its subscription and no write actions; an open event detail updated through polling. C14 compatibility also passed three real Worker paths: manual, actual scheduled triggering and Webhook.

Chrome additionally exposed an ordinary RunOnce button and Task status-label mismatch during the subscription lock. The backend rejected the negative trigger test without creating an Execution. The `d48e3f8` Web correction passed component tests, independent review and a bilingual Chrome check on its matching deployment. Catalog and Header showed the subscription lock; RunOnce was actually disabled, while save, Input and Worker remained locked. The ordinary manual Adapter retained an enabled run button without another execution. Enabling and stopping latest reception in the new epoch preserved old custody and unique target writes. Runtime files and Web artifacts had separate hash verification.

The first capacity run used one ordinary Python Adapter, four partitions, two Worker execution slots and the existing 1-second RabbitMQ minimum redelivery delay. It offered 1,800 events over 1,800 seconds; 1,732 target writes completed during that phase, about 0.962 events/second, with growing backlog. It therefore failed the sustained 1-event/second gate. Complete post-production convergence to 1,800/1,800 does not change that result. The sample added about 14.81 kB of physical Control database storage per event. Linear projection at 1 event/second is neither achieved throughput nor retained steady-state storage. At that hypothetical rate the default 10,000-row history budget fills in about 2 hours 46 minutes 40 seconds; it does not promise seven days of uninterrupted reception.

The existing `DLR_RABBITMQ_RETRY_BASE_SECONDS=0.1` setting was applied to the empty development queue in an exclusive quiet window with applications stopped and consecutive zero connection, consumer and message checks. RabbitMQ 4.3.5 quorum queues do not support conditional deletion, so this maintenance had no atomic server-side empty-queue guard. Product defaults remain unchanged.

The revised configuration fixed `d48e3f8`, one Adapter, four partitions, a 1-second tick and two configured execution slots. Excluding warmup, all 1,800 events offered over approximately 1,800 seconds reached the target before the phase ended. Independent review checked every complete payload, source position and single effective write, with no accumulating backlog; this specific 30-minute steady gate passed. Send-to-insert time for ultimately committed target rows had a 1.98-second p95. Application buffer peak was 3,501 bytes. Control RSS still had a small positive slope of about 32 kB/min over the last ten minutes, which does not establish absence of leaks or arbitrary-duration stability. This result does not extend to the default 1-second redelivery setting or seven-day operation. A finite burst was followed by 600 seconds of reduced-quota observation and explicit recovery. The 103 inputs ended as 71 successes, two explicit skips and 30 unaccepted records at the history limit. The observation window met the two-times API threshold; the ten runtime-status samples during the short burst had a 2.604-times p95, retained as a transient finding. Accepted custody drained and listeners closed. History remains full until legitimate governance permits recovery; limits were not raised and payload retention was not shortened. Versions, first failures, growth and unverified scope are recorded in the [B3 review and acceptance record](../../openspec/changes/add-event-subscriptions/b3-review.md).

## Migration and application rollback

Forward migration `0047_issue219_subscriptions` adds subscription structures and extends the trigger constraint. `0048_issue219_kubernetes` adds immutable source identity and persistent batch/control/abandonment accounting. They create no default subscriptions and do not rewrite existing Adapter inputs. Legacy Kubernetes configurations without a verified identity require an explicit new epoch. Stop writers/reception, create and verify a backup, migrate forward, then update Control/Web. Existing protocol-v3 JSON Workers need no new input type.

Before reverting application binaries, disable subscriptions, verify actual listener closure, and settle or explicitly preserve pending custody, nonterminal executions and unresolved barriers. Turning the feature off still allows existing custody to drain. A feature flag, healthy container or empty dispatch queue alone does not establish that custody is clear. Retain the new tables/audit; never automatically downgrade or restore the database. Downgrade refuses existing subscription history to avoid deleting acknowledged responsibilities.

Local development endpoints, Compose parameters, credentials, backups and raw evidence remain private. Source support and measured capacity follow each source's acceptance record.
