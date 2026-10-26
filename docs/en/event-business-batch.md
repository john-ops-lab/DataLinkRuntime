# Processing several events in one run

HTTP data push and RabbitMQ sources offer an explicit **Several events per run (code support required)** mode. A run processes its group in order; every event retains its own receiving record, identity, and result. The default remains one event per run. Kafka, RocketMQ, and Kubernetes do not offer this business processing mode.

## Configure it inside the adapter

1. Stop reception and settle existing events before changing the mode.
2. Prepare compatible code in the editor. Expand **Multi-event code support**, explicitly select **This code supports several events per run**, and save a new version. The checkbox only changes the version draft; it does not change code, save a version, or start a run automatically.
3. Under **Run settings → Configure source → Advanced settings / Advanced connection settings**, select the multi-event mode and save it. Reception stays stopped.
4. Save the worker and other runtime settings, then explicitly start reception. DLR checks the saved code declaration and refuses incompatible code. Existing responsibility remains protected.

The declaration corresponds to `{"event_batch_schema":"dlr.event.batch.v1"}` in the version's `runtime_config`. Other parameters remain intact. Invalid JSON and non-object drafts are not replaced by this control. Existing dirty-state, permission, and receiving/draining locks still apply.

## Code input and results

The input schema is `dlr.event.batch.v1`. Its ordered `members` contain `receipt_id`, `delivery_id`, `delivery_ordinal`, `member_index`, and `input`. A member's `input` is its original complete event: HTTP data is in `data.payload`, and RabbitMQ original bytes are base64 in `data.value`.

Report success only after the actual business operation and idempotent commit. Stop at the first failed or unknown result. The response shape is:

```python
result = {
    "schema": "dlr.event.batch.result.v1",
    "batch_id": input["batch_id"],
    "manifest_hash": input["manifest_hash"],
    "members": [
        {
            "receipt_id": member["receipt_id"],
            "delivery_id": member["delivery_id"],
            "delivery_ordinal": member["delivery_ordinal"],
            "member_index": member["member_index"],
            "status": "succeeded",
            "error_code": None,
        },
        # Include every member's actual result, in the original order.
    ],
}
```

This fragment explains the shape; it is not evidence that a business write succeeded. The source drawer contains a sequential handling skeleton whose unfinished business function explicitly fails. A PostgreSQL implementation should use stable keys and content verification so retries do not duplicate effective writes. Arbitrary business code must implement its own protections.

A valid response contains either all successes, or a successful prefix, one failed/unknown member, and only `not_run` members afterward. Use `predecessor_failed` for that unprocessed suffix; successful members use `error_code=None`. Do not omit, duplicate, reorder, or add member identities. Keep business payloads out of results. Missing, truncated, or invalid output proves no member successful.

Each group is bounded to 100 members, 256KiB of complete execution input, and 50ms of assembly waiting. Envelope overhead can reduce the actual member count. The adapter still has at most one active Attempt.

## Results and cancellation

Use the existing **Execution history → Received events** view. Each event shows its own result; the group-count link opens the shared run. Execution details explain its member count and cancellation scope.

| Result | Meaning and action |
| --- | --- |
| Processing succeeded | A trustworthy successful result exists for this event |
| Processing failed | Inspect the reported cause, fix it, then process the original ID again |
| Result unconfirmed | The target may already have been written; check it before processing the original ID and body again |
| Not processed | An earlier member did not succeed, or the run provably never started; resolve the predecessor before processing this event |

Cancelling the shared run affects every unfinished member. It does not cancel just the selected event or roll back the target. Success already validated by Control remains; unconfirmed effects require verification.

The first version retains individual replay, repair, and explicit skip with original failures and audit. A replay of one member still receives a one-member batch input. Current-configuration replay across processing modes is refused; use the original runtime settings, or create a new data request only after reviewing its meaning.

## Verification boundary

This page defines behavior and operation. A support declaration, created Execution, accepted HTTP response, Broker confirmation, or successful process exit does not replace per-event business results. Capacity and recovery claims require the corresponding real resource-bounded run and independent reconciliation.

See [event reception](event-subscriptions.md) and [durable HTTP intake](durable-http-ingress.md).
