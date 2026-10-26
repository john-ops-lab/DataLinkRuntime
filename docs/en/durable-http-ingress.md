# Durable HTTP ingress

Use the HTTP source when an upstream system pushes independent JSON messages to DLR. DLR stores the input and its processing responsibility before the adapter executes. **HTTP 202 confirms durable receipt; it does not confirm business success.**

This is separate from the existing Webhook interface. `/api/hooks/{public_id}` retains its `accepted` / `completed` response policy, `execution_id`, and result waiting behavior. Existing Webhooks are not migrated automatically.

## Configure and enable

1. A deployment administrator explicitly enables event sources and `DLR_HTTP_INGRESS_ENABLED=true`. HTTP ingress is disabled by default. Deployment capacity still controls the upper limits.
2. In a task adapter's run settings, select event triggering and the HTTP source. Select an existing account credential of type `token`; only its reference is stored.
3. Save the source to obtain the relative `ingress_path`. Saving the source or code and checking reception configuration do not start reception or send simulated business messages.
4. Save code, select the fixed worker, and explicitly enable reception. An effective Runtime owner is required; new reception returns 503 while ownership is not ready.

Configuration stays inside the adapter. No separate subscription navigation is added. The source token permits reception and safe status lookup for its own source only; it grants no management, replay, skip, or reset access.

Stopping HTTP reception invalidates its passive lease in the same transaction, including a lease left by a crashed process. Accepted records continue processing. Unfinished request jobs retain their original permits and memory until they actually end. Existing code, credential and source configuration locks remain while responsibility is unfinished.

## Send one JSON value

`POST /api/ingress/{public_id}` requires:

- `Authorization: Bearer <source token>` using the current credential. The administrator token cannot replace it.
- `Content-Type: application/json`, optionally specifying UTF-8. Only uncompressed UTF-8 bodies are accepted.
- `Idempotency-Key`: a stable upstream message ID of 1–128 printable ASCII bytes.
- A single JSON value. Objects, arrays, strings, numbers, booleans, and `null` are supported. In per-event processing, adapter code reads the complete parsed value from `input.data.payload`; business-batch input is described below. Values are not silently cleaned or truncated.

The variables below are placeholders. Retain the original file rather than reserializing it for each attempt:

```sh
curl --request POST "$DLR_URL$INGRESS_PATH" \
  --header "Authorization: Bearer $SOURCE_TOKEN" \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: upstream-message-000001' \
  --data-binary @message.json
```

Content equality uses SHA-256 of the original body bytes. `{"value":1}` and `{"value": 1}` differ. Changing whitespace, property order, or numeric spelling while reusing an ID can conflict. Equal bodies with different IDs are distinct valid messages.

## Responses and safe retries

| Response | Meaning and sender action |
| --- | --- |
| `202` | The first receipt committed durably. Retain `receipt_id` and the relative `status_url`, then query processing status. `execution_id` may still be `null`. |
| `200` | With bounded request resources available and current authentication and byte comparison successful, the same ID and original bytes read back the existing receipt and status. No new initial responsibility or capacity charge is created. |
| `401` / `404` | Authentication is invalid or the route/record is not visible. Check the source credential and address. |
| `409 event_identity_conflict` | The ID already identifies different original bytes. Investigate ID reuse; do not blindly assign a fresh ID to bypass it. This HTTP rejection does not permanently block other valid messages. |
| `409 ingress_disabled` | New reception is stopped. Wait for explicit enablement; accepted messages continue draining. |
| `410 ingress_epoch_retired` | The address is retired and contains no existing receipt for this ID. Use the current address for new messages. |
| `413` / `415` / `422` | Size/structure, format/compression, JSON, or ID is invalid. Correct the request. |
| `429` / `503` | Request resources, durable reception capacity, or platform availability are temporarily insufficient. A duplicate can hit a resource 429 before readback. Respect `Retry-After` and retry the same ID and original bytes within a finite budget; this does not prove the ID was never accepted. |
| Timeout/disconnect | This does not prove the transaction failed to commit. Retain and retry the same ID/body; query if a receipt ID is known. |

Application 429/503 responses supply a bounded 1–30 second `Retry-After`; the current implementation uses 1 second. Senders need durable pending storage, finite concurrency, retry deadlines, and alerting. Full durable pending/history quotas never recharge an existing message or discard its responsibility. However, a duplicate POST can first receive 429 when request, body, or native-work resources are full. Retain the same ID and original bytes for retries, or query a known `receipt_id`. Remove a message from the unconfirmed sender queue only after a durable receipt response or successful readback. Business success remains a separate result.

`GET /api/ingress/{public_id}/receipts/{receipt_id}` uses the route's current valid source token. It returns receipt time, processing state, execution association, and a safe error code. It exposes no raw input, code, business output, or credentials. `pending` means waiting, `processing` means in progress, and `succeeded` / `failed` and other states describe the retained responsibility.

## Routing, credentials, and bindings

- A generated address permanently identifies its original subscription and source epoch. It cannot be reassigned, and deleted addresses are never reused.
- With bounded request resources available and current authentication valid, stopping reception preserves original-ID/body readback and status lookup. Full durable pending/history quotas, a disabled feature, or an unavailable worker, internal broker, or reception owner do not change this rule, recharge the message, or discard its responsibility.
- Explicit reset creates a new address. During retention, the old address only reads back existing receipts; an unknown ID returns 410 instead of becoming a new-epoch responsibility. Deletion closes external access. A non-reusable marker survives permanent adapter history deletion.
- Token rotation revokes the old token for historical lookup too. Ordinary source/credential changes require stopping, confirmed closure, and drained responsibility. Changing the token reference in an HTTP configuration does not itself require a new address.
- Each accepted message retains its immutable revision, fixed worker, credential references, and activation policy. Waiting or recovery does not silently switch to current code. Credential plaintext is absent from messages and snapshots.

## Resources and retention

The body limit is 64 KiB and the complete event limit is 256 KiB. JSON depth is at most 32 and total nodes at most 4,096, including object member names. Duplicate object members, nonstandard/nonfinite numbers, and strings or numbers that cannot be stored losslessly are rejected. Reading has a 5 second total and 2 second idle limit, including unknown-length bodies. Compression is unsupported.

HTTP permits at most 32 requests and 8 MiB of combined body/event reservations, within the shared 64 MiB source buffer budget. HTTP parsing and database work permit at most 8 concurrent native jobs. HTTP and native event sources share one `intake` database pool of at most 8 connections; management and recovery have a separate reserve of at most 4 connections. Neither pool allows overflow connections. Exhaustion rejects rather than queues without bounds. Cancellation retains the permit until native work actually ends.

Each request reserves 320 KiB for the body and complete event limits, so the byte budget can hold at most 25 such reservations. When existing request, byte, or native-work limits are full, a reception POST, including a duplicate, can receive `429 ingress_capacity_exceeded` with `Retry-After: 1` before authentication, byte comparison, or durable readback. These limits already exist in the request path; the ASGI shortcut only returns the same refusal earlier. It does not use the key to bypass authentication or byte comparison, add a pool, or queue without bounds. Status and management requests keep their original paths. GET lookup still has bounded request and native-work capacity; it is not an unlimited bypass. Early refusal or client cancellation does not release another request's reservation.

Raw inputs default to 7 days and deduplication metadata to 30 days. Unfinished, failed, or recovery-referenced data remains protected. The sender's agreed retry horizon must fit the advertised deduplication window; permanent deduplication is not promised outside it. Original bytes, complete events, and required retained copies count toward capacity.

## Database storage observation

Default Compose mounts the same `dlr_pg_data` volume used by PostgreSQL read-only at `/var/lib/dlr/ingress-storage` in Control. `DLR_INGRESS_STORAGE_ROOT` defaults to that anchor. Observation reads free space without initializing, writing, or changing permissions on database files. Free space in the artifact volume is not a substitute for database capacity.

Standalone Control or an external database requires an explicitly configured, existing absolute directory that demonstrably represents the database's actual capacity domain. Overriding the PostgreSQL data volume or database address also requires checking Control's observation mount and this setting; changing a path string alone does not establish capacity equivalence. A missing, empty, relative, non-directory, or unreadable path closes new reception as storage unavailable. The application neither creates the directory nor falls back to its working directory or root filesystem. Existing responsibility still drains and original receipts remain readable. Explicit old `.env` values pointing to artifacts or other paths are preserved; review and update them during upgrade.

The default topology keeps data and WAL together. Custom WAL, tablespaces, separate disks, and managed database quotas require coverage of every relevant capacity domain. An arbitrary local directory cannot establish remote database free space. Keep new reception disabled without a proven equivalent mount or authoritative capacity source. The 5 GiB stop and 6 GiB resume thresholds remain unchanged; a path setting is not capacity acceptance.

## Reception groups and business batches

When business-batch processing is explicitly selected, the runtime may combine at most 16 HTTP requests or RabbitMQ messages into one reception transaction, still with at most 1 MiB of original bodies plus complete events. No additional setting is needed. Additional collection waiting is at most 25 ms for HTTP and 5 ms for RabbitMQ. Reaching any item, byte, or time limit seals the group; later requests cannot join it. Individual decoding and validation still count toward reception time. After its deadline, RabbitMQ may drain only already complete SDK deliveries, with no further I/O or waiting. Ordinary per-event processing keeps its existing path.

After collection, an HTTP group's ready waiting before submission is bounded to 1 second while retaining its original request and byte reservations. Groups for the same reception route submit in collection order; other routes may run concurrently within the existing 8 native-work slots, without additional threads, connections, or memory limits. A group that has not begun submission receives 429 on expiry or 503 on closure, both with `Retry-After: 1`. Once submission starts, neither this wait deadline nor an individual request cancellation releases its reservations early. Responses remain independent per request, and successful custody still requires a confirmed database commit.

RabbitMQ native credit is capped at 16 within the existing 2 MiB owner and shared 64 MiB application budgets. Each new window pauses further prefetch until its first delivery has obtained preparation workspace, so large messages cannot fill the owner buffer before any valid prefix can progress. If the next read cannot obtain budget, a prepared valid prefix can commit and be acknowledged with reception paused before reading resumes; budget refusal does not create a poison-message barrier. Unsupported SDK guards retain single-message reception. Actual group size remains byte- and budget-bound; a full group or a configured maximum is not capacity acceptance.

Every message retains its own identity, receipt, and processing responsibility. HTTP sends 202/200 only after a confirmed transaction commit and reports each request's errors independently. RabbitMQ ACKs only the durably accepted, contiguous valid prefix after confirmed commit; an uncertain commit permits no ACK. Safe retries, deduplication, and individual confirmation rules remain unchanged.

[Business batches](event-business-batch.md) define how adapter code handles multiple inputs in one execution, with at most 100 members, a complete input of at most 256 KiB, and a result for every member. Reception groups reduce repeated reception-transaction work and may contain different members from an execution batch. A reception commit does not establish business success for any member.

## Upgrade and rollback

The migration adds a nullable raw-input column, immutable HTTP routing, and capacity audit without rewriting existing source identities, hashes, or inputs. Back up first and validate upgrade over pending, failed, and Kubernetes control responsibilities. New HTTP reception remains disabled by default.

Before rollback, stop new reception and drain or preserve readable responsibility. Use an application version compatible with the retained source and schema. Once routing or capacity audit exists, downgrade refuses to delete it. Do not clear data to force rollback. Actual functional, capacity, and recovery conclusions belong to the corresponding measurement report; configuration documentation is not proof of capacity acceptance.
