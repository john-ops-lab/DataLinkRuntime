# Infrastructure Incident Dispositions

[简体中文](../zh-CN/incident-dispositions.md) · **English**

Infrastructure Incidents in execution details record delivery failures. Repair the
Broker, Worker, capacity, or routing problem first, then use the capabilities shown
for the original execution. Recovery keeps its Execution ID, input, and version;
it does not create a Replay. Verify that original record's result, Attempts, and
resource cleanup after submission. An accepted disposition does not prove execution success.

## UI Actions

Users with adapter edit access can submit dispositions. Read-only users can inspect
reasons and audit receipts. The server rechecks permissions, message generation,
current state, and frozen materials at submission. Displayed capabilities are
advisory: if the execution was claimed, cancelled, or completed meanwhile, use the
submission response and refresh the original record.

| Original state | Meaning of the action |
| --- | --- |
| Current generation queued, no active Attempt, materials intact | Recover the original record; reuse a pending Outbox delivery, and create a new generation only for an eligible published delivery |
| Running | Recovery is unavailable; termination requests cooperative cancellation and waits for Worker/cleanup convergence |
| Cancellation requested or waiting for retry | Recovery is unavailable; termination follows existing cancellation rules |
| Execution terminal, Incident still open | Verify and close the Incident with an audit receipt; do not rewrite the execution result or add an Attempt |
| Incident from an older generation | Do not recover the old message; termination ignores that Incident without cancelling the current generation |
| Unverifiable message identity, missing original material, or conflicting state | Reject recovery with a reason; repair and reread the original record without replacing historical snapshots |

Observation count measures DLQ observations. Disposition count measures distinct
manual audit receipts. Recovery dispatch count measures recoveries that actually
created a new generation. Reading details or retrying the same idempotent request
does not increment these counts.

## API and Retries

`GET /api/executions/{execution_id}/reliable-detail` returns current capabilities.
Submit to `POST /api/executions/{execution_id}/incidents/{incident_id}/dispositions`
with a UUID `Idempotency-Key` header and the closed request body:

```json
{
  "action": "recover",
  "expected_generation": 1,
  "reason_code": "capacity_repaired"
}
```

Actions are `recover` and `terminate`. Reasons are `capacity_repaired`,
`routing_repaired`, `operator_cancel`, and `verified_terminal`. Use the generation
you just read, rather than copying the example value. Authentication determines the
actor; requests cannot supply one. Account entry retains existing Session/CSRF requirements.

After a network interruption or duplicate click, keep the same key and body for
the original intent. The original `receipt` retains its ID and request identity.
Cooperative cancellation result fields converge in place at the actual terminal
transition; the outer `execution_status` may also reflect subsequent progress. Changing the body with the
same key returns `409`. Refresh after a state conflict; generate a new key only
for an explicitly new disposition intent.

`200` reports a determined disposition outcome; `202` reports a cooperative
cancellation request. `409` reports an eligibility conflict and `503` an Outbox
capacity limit. Inspect `receipt.code` and honor `Retry-After` when present.
`GET` on the same disposition path lists audit receipts with `limit=1..100` and
the returned `next_before_id` UUID cursor. Reading does not create audit receipts.

## Broker and Upgrade Boundaries

A Worker stops receiving new messages when all slots are busy. Normal capacity
waiting should not consume `delivery_limit`. Control classifies structured
`x-death` entries for the target Worker queue: `delivery_limit` requires manual
disposition. `rejected`, `expired`, `maxlen`, and unknown reasons retain existing
automatic validation; only an eligible current queued message may return to its
same-generation Outbox. Queue configuration headers are not evidence of an actual
delivery-limit death.

Migration `0040_issue152_dispositions` adds disposition audit storage without
inventing historical manual actions. A local preview upgrade carrying unfinished
responsibility must use the explicit preservation manifest in the
[local preview controller](local-preview.md). Retain the original database,
volumes, journals, and Admission. Report historical cleanup as completed only
after the new Worker's actual receipt. Do not obtain a passing result by emptying
the DLQ, deleting Incidents, editing terminal states, or removing persistent volumes.
