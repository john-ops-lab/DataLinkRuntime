"""Retained input copies have separate lifetimes and prepaid finite budgets.

Metadata allowances are conservative structural UTF-8 JSON bounds, not physical
PostgreSQL page sizes. No source payload, raw input or credential is included.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import inspect

from dlr.control.services.adapter import domain_error

RECEIPT_METADATA_BYTES = 4096
DELIVERY_METADATA_BYTES = 512
BATCH_METADATA_BYTES = 8192
CONTROL_ENVELOPE_BYTES = 8192
CONTROL_RESERVATION_BYTES = (
    2 * CONTROL_ENVELOPE_BYTES + RECEIPT_METADATA_BYTES + DELIVERY_METADATA_BYTES
)


def responsibility_bytes(envelope_bytes: int, raw_bytes: int = 0) -> int:
    return 2 * envelope_bytes + raw_bytes + RECEIPT_METADATA_BYTES + DELIVERY_METADATA_BYTES


def compact_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def validate_metadata(row: Any, maximum: int) -> None:
    # Batch-only fields have their own prepaid allowance. Null additions must
    # not shrink the existing single-message structural identity contract.
    skipped = {
        "envelope",
        "source_raw_body",
        "batch_execution_id",
        "batch_member_outcome",
        "batch_member_code",
        "retained_batch_bytes",
    }
    values = {}
    for column in inspect(type(row)).columns:
        if column.name in skipped:
            continue
        value = getattr(row, column.name)
        if isinstance(value, datetime):
            value = value.isoformat()
        if value is None and (column.name == "id" or column.name.endswith("_id")):
            value = 9223372036854775807
        values[column.name] = value
    if compact_bytes(values) > maximum:
        raise domain_error(
            422, "event_metadata_too_large", "Event metadata exceeds its prepaid bound"
        )


@dataclass(frozen=True)
class BatchBudget:
    input_bytes: int
    batch_bytes: int
    total_bytes: int


def batch_budget(envelope: dict[str, Any], raw_bytes: int = 0) -> BatchBudget:
    from dlr.control.services.subscription_batch_protocol import MAX_INPUT_BYTES, singleton_reserve

    future_input = singleton_reserve(envelope)
    if future_input > MAX_INPUT_BYTES:
        raise domain_error(
            422, "event_payload_too_large", "One complete batch member exceeds 256 KiB"
        )
    total = (
        compact_bytes(envelope)
        + raw_bytes
        + RECEIPT_METADATA_BYTES
        + DELIVERY_METADATA_BYTES
        + future_input
        + BATCH_METADATA_BYTES
    )
    return BatchBudget(future_input, BATCH_METADATA_BYTES, total)


def receipt_budget(
    receipt: Any, envelope_bytes: int, raw_bytes: int = 0, *, batch: BatchBudget | None = None
) -> None:
    receipt.retained_payload_bytes = envelope_bytes + raw_bytes
    receipt.retained_metadata_bytes = RECEIPT_METADATA_BYTES
    receipt.payload_bytes = (
        batch.total_bytes if batch else responsibility_bytes(envelope_bytes, raw_bytes)
    )
    validate_metadata(receipt, RECEIPT_METADATA_BYTES)


def delivery_budget(
    delivery: Any, envelope_bytes: int, *, batch: BatchBudget | None = None
) -> None:
    delivery.retained_execution_bytes = batch.input_bytes if batch else envelope_bytes
    delivery.retained_batch_bytes = batch.batch_bytes if batch else 0
    delivery.retained_metadata_bytes = DELIVERY_METADATA_BYTES
    validate_metadata(delivery, DELIVERY_METADATA_BYTES)
