# Start and stop adapters in batches

[简体中文](../zh-CN/adapter-batch-operations.md) · [Product guide](product.md)

Open batch selection in the adapter catalog, select adapters, then choose Start selected or Stop selected. Review each proposed action before confirming. A batch contains at most 100 adapters and processes them one at a time. One failure does not undo other items.

Select current list includes only visible, editable adapters. Changing a search or filter removes hidden items from the selection. Narrow the filter if the list exceeds the limit. Selecting a checkbox does not navigate away from the current workbench; adapter names keep their existing navigation behavior.

| Run method | Start | Stop |
| --- | --- | --- |
| Manual | Run once with saved code and input | Request cancellation of unfinished executions fixed when this operation was prepared |
| Scheduled | Enable the existing schedule without an extra immediate run | Stop creating new scheduled executions |
| Webhook | Accept new requests | Stop accepting new requests |
| Event trigger | Receive events using the existing source configuration | Stop new intake and wait for the listener to close |

Batch actions use **saved content**. Unsaved code, input, or run settings remain drafts. Actions do not replace the runtime worker, credential bindings, or run method. Stopping a schedule, Webhook, or event source preserves work already accepted and does not clear its backlog. Manual cancellation targets only the executions fixed during preparation, never executions created afterward. Cancellation does not reverse external writes already performed.

## Understand the results

- **Execution submitted** means an execution was created. Inspect its execution result separately; submission does not mean successful data processing.
- **Stopping / cancellation requested** means the request was accepted but closure has not been confirmed.
- **Receiving stopped / schedule disabled** means that action completed. Previously accepted work follows its existing processing rules.
- **No change needed** means the target state already holds, without another activation.
- **Failed** means that item did not complete. Open the adapter to correct its configuration or resolve the reported problem. Only recoverable failures can be retried.
- **Result unconfirmed** means a connection interruption or missing evidence prevents confirmation. Query the original operation before starting another batch.

Retry failed items processes only definite, actionable failures and preserves successful results. Changes to saved code, input, worker, or trigger settings after preparation produce a configuration conflict. Refresh the catalog and explicitly create a new operation to use the new configuration. Sources requiring repair or identity verification must be handled from their adapter; batch start does not reset a source.

After refreshing the page, you can query the batch result again. The browser stores only an operation identifier scoped to the current account. The server checks current permissions when returning results and before each action. Read-only adapters cannot be changed through batch selection.

## API and upgrade

Read `batch_token` from the adapter response. Prepare with a stable UUID, `start|stop`, and a list of `{adapter_id, expected_token}` targets:

```text
POST /api/adapter-batches
GET  /api/adapter-batches/{id}
POST /api/adapter-batches/{id}/execute
POST /api/adapter-batches/{id}/retry   {"adapter_ids": [123]}
```

Creation only prepares the plan. Execute processes uncommitted items; GET never performs actions. Keep the original ID and parameters when retrying a request. A request timeout is not proof that an action failed. The first implementation submits state changes serially and reports actual listener closure through result queries.

Deployment applies the batch-record migration after 0049. Back up first and preserve existing volumes, tokens, and the master key. The new tables do not rewrite adapters or executions. An application rollback must preserve operation records and existing responsibilities; do not downgrade or restore an old database to erase work already accepted. Batch deletion, configuration editing, and cross-adapter workflows are outside this feature.
