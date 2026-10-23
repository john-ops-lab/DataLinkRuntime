# Event subscriptions: management and durable custody

The feature defaults to `DLR_EVENT_SUBSCRIPTIONS_ENABLED=false`. B2 includes the Kafka connector and isolated protocol probes; the public available-source registry remains empty. `GET /api/event-subscriptions/capabilities` reports the actual flag and delivered sources. The internal implemented registry permits connection diagnostics separately from public enablement. Kafka requires B3 management, business-result and capacity acceptance before being exposed; Kubernetes follows B4/B5. A successful connection test establishes neither delivery readiness nor business success.

## Management and authorization

One non-deleted subscription binds one Task/manual Adapter. Reads use its view/read permission, mutations require edit, and selecting source credentials follows existing owner/admin rules. Lists filter authorization before counting and pagination. Configuration contains credential references; passwords, tokens and private keys are not accepted as source configuration.

All routes use `/api/event-subscriptions`:

| Method and suffix | Behavior |
| --- | --- |
| GET `/capabilities` | Actual flag and available sources |
| GET/POST root | Authorized listing or a new disabled subscription |
| GET/PATCH/DELETE `/{id}` | Detail, configuration/metadata update, or deletion after stopping and draining |
| POST `/{id}/enable`, `/{id}/disable` | Start/stop reception; accepted custody continues draining |
| POST `/{id}/test-connection` | Bounded diagnostic for an implemented source, requiring the feature flag, edit permission and expected version; no business execution |
| POST `/{id}/clone` | Disabled copy bound to another editable Adapter, without progress |
| POST `/{id}/reset` | Audited request ID/reason and a new epoch after stopping/draining |
| POST `/{id}/repair/enter`, `/repair/revisions`, `/repair/exit` | Restricted code repair with backlog |
| GET `/{id}/events`, `/{id}/events/{receipt_id}` | Event and original/replay execution chain |
| POST `/{id}/events/{receipt_id}/replay`, `/skip` | Idempotent replay or an explicit unprocessed disposition |
| GET `/{id}/quota-audit`, POST `/{id}/quota-repair` | Read-only accounting audit or explicit repair after disabling and confirming listener closure |

Runtime-affecting operations include `expected_config_version`; stale versions return `subscription_config_conflict`. Replay/skip/reset also require `request_id` and a nonempty `reason`, and request IDs cannot be reused with different semantics. Name/description updates are independent. Lists use bounded `before_id` pagination.

Normal manual tests retain the saved Task InputConfig. Backend guards block runtime mutations and ordinary triggers while a subscription receives, stops, drains or repairs. A bound subscription must be explicitly deleted after stopping/draining before changing the Task trigger mode or deleting its Adapter.

## Durable delivery and recovery

T1 atomically commits the complete Envelope, custody, quotas and safe source position before source acknowledgment. T2 atomically transfers pending custody to Admission, Execution, Outbox and the delivery link. Queue unavailability leaves custody pending. Database-time leases and generation fencing reject stale owners; listener permits remain occupied until the native connection actually closes.

Execution uses `trigger=subscription` and the existing JSON protocol, with public snapshot `{source_type: json, revision: null}`. Event input never overwrites the saved Task input. Original activation Revision, Worker and credential references remain frozen; secret values are not snapshots. Subscription replay uses its own authorized action, not ordinary Execution replay.

Repair stops reception and new dispatch, waits for nonterminal executions to settle, and creates an immutable code/dependency-only repair Revision. An explicitly selected replay may use it for the failed event. The original activation and later pending events retain their original version.

Pending, retained payloads, deduplication metadata and receipt rates have independent limits and low-water recovery. Default payload/dedupe windows are 7/30 days; unfinished custody, failure barriers and active batch references remain protected. Expired payload replay returns `event_payload_expired`. Logical quotas do not measure physical database/log storage.

Background accounting audits use bounded batches and never automatically change custody or counters. After disabling reception and confirming actual listener closure, an editor may submit `quota-repair` with the expected version, request ID and reason. It recomputes this subscription's ledger from durable responsibilities and rebuilds the shared aggregate. Pending custody may remain so a corrupt counter cannot make draining and repair mutually impossible. Events and Executions remain unchanged.

## Kafka configuration and connection diagnostics (B2)

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

With the flag enabled, connection testing can inspect implemented Kafka even while public enablement remains unavailable. Supply `expected_config_version`; the response contains verified `source_type`, `cluster_id`, topic/partition counts and group availability. Testing does not join a group, commit offsets, write checkpoints or execute an Adapter. SQL preparation, decryption and transaction release run in the synchronous API pool; source RPCs use the dedicated budget. Responses exclude source addresses and secret values.

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

### Listener budgets and closure

Control shares at most 16 source-owner/connection-test permits, without a waiting queue, and one actual owner per subscription. SDK I/O threads and connections are counted separately. Source waiting occupies neither ordinary Worker Slots, the default API pool nor its event loop. Application buffer accounting is shared at 64 MiB, with 2 MiB per owner; raw records and final Envelopes also obey their own and Execution JSON size limits. Native fetch/frame limits do not impose a hard ceiling on decompression or process RSS; B3 must measure capacity.

Database leases last 30 seconds and renew independently every 10 seconds. Temporary connection errors use jittered exponential delays of 1–60 seconds; capacity retries use 1–30 seconds and require low-water recovery. Definite authentication, authorization and identity failures await explicit action. Connection-test HTTP waiting defaults to 15 seconds. Timeout/cancellation ends that wait only; the permit remains occupied until native work actually returns and closes. Failed closure retains the permit; repeated enable cannot bypass it.

Consumer close disables automatic commit. Explicit commit/Consumer close use a 3-second client budget; normal Consumer requests use 10 seconds. Three seconds is not an absolute bound on the entire stop operation or arbitrary OS/NSS behavior, and excludes DNS resolution and sequential RPCs. Resolver/platform behavior must remain within the B0 verified boundary. Turning reception off preserves draining under the current Control; reverting old binaries or removing tables must not erase backlog.

## Migration and application rollback

Forward migration `0047_issue219_subscriptions` adds subscription structures and extends the trigger constraint. It creates no default subscriptions and does not rewrite existing Adapter inputs. Stop writers/reception, create and verify a backup, migrate forward, then update Control/Web. Existing protocol-v3 JSON Workers need no new input type.

Before reverting application binaries, disable subscriptions, verify actual listener closure, and settle or explicitly preserve pending custody, nonterminal executions and unresolved barriers. Turning the feature off still allows existing custody to drain. A feature flag, healthy container or empty dispatch queue alone does not establish that custody is clear. Retain the new tables/audit; never automatically downgrade or restore the database. Downgrade refuses existing subscription history to avoid deleting acknowledged responsibilities.

Local development endpoints, Compose parameters, credentials, backups and raw evidence remain private. Source support and measured capacity follow each source's acceptance record.
