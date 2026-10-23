# Event subscriptions: management and durable custody

The shared foundation defaults to `DLR_EVENT_SUBSCRIPTIONS_ENABLED=false`. B1 exposes no usable source. `GET /api/event-subscriptions/capabilities` reports the actual feature flag and delivered sources. Kafka and Kubernetes each require connector and business-result acceptance before becoming available.

## Management and authorization

One non-deleted subscription binds one Task/manual Adapter. Reads use its view/read permission, mutations require edit, and selecting source credentials follows existing owner/admin rules. Lists filter authorization before counting and pagination. Configuration contains credential references; passwords, tokens and private keys are not accepted as source configuration.

All routes use `/api/event-subscriptions`:

| Method and suffix | Behavior |
| --- | --- |
| GET `/capabilities` | Actual flag and available sources |
| GET/POST root | Authorized listing or a new disabled subscription |
| GET/PATCH/DELETE `/{id}` | Detail, configuration/metadata update, or deletion after stopping and draining |
| POST `/{id}/enable`, `/{id}/disable` | Start/stop reception; accepted custody continues draining |
| POST `/{id}/test-connection` | Connection check for a delivered source; no business execution |
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

## Migration and application rollback

Forward migration `0047_issue219_subscriptions` adds subscription structures and extends the trigger constraint. It creates no default subscriptions and does not rewrite existing Adapter inputs. Stop writers/reception, create and verify a backup, migrate forward, then update Control/Web. Existing protocol-v3 JSON Workers need no new input type.

Before reverting application binaries, disable subscriptions, verify actual listener closure, and settle or explicitly preserve pending custody, nonterminal executions and unresolved barriers. Turning the feature off still allows existing custody to drain. A feature flag, healthy container or empty dispatch queue alone does not establish that custody is clear. Retain the new tables/audit; never automatically downgrade or restore the database. Downgrade refuses existing subscription history to avoid deleting acknowledged responsibilities.

Local development endpoints, Compose parameters, credentials, backups and raw evidence remain private. Source support and measured capacity follow each source's acceptance record.
