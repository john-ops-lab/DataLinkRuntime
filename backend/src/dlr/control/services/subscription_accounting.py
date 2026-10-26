"""Retained input copies have separate lifetimes and prepaid finite budgets.

Metadata allowances are conservative structural UTF-8 JSON bounds, not physical
PostgreSQL page sizes. No source payload, raw input or credential is included.
"""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import inspect

from dlr.control.services.adapter import domain_error

RECEIPT_METADATA_BYTES = 4096
DELIVERY_METADATA_BYTES = 512
CONTROL_ENVELOPE_BYTES = 8192
CONTROL_RESERVATION_BYTES = (
    2 * CONTROL_ENVELOPE_BYTES + RECEIPT_METADATA_BYTES + DELIVERY_METADATA_BYTES
)


def responsibility_bytes(envelope_bytes: int, raw_bytes: int = 0) -> int:
    return 2 * envelope_bytes + raw_bytes + RECEIPT_METADATA_BYTES + DELIVERY_METADATA_BYTES


def compact_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def validate_metadata(row: Any, maximum: int) -> None:
    skipped = {"envelope", "source_raw_body"}
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


def receipt_budget(receipt: Any, envelope_bytes: int, raw_bytes: int = 0) -> None:
    receipt.retained_payload_bytes = envelope_bytes + raw_bytes
    receipt.retained_metadata_bytes = RECEIPT_METADATA_BYTES
    receipt.payload_bytes = responsibility_bytes(envelope_bytes, raw_bytes)
    validate_metadata(receipt, RECEIPT_METADATA_BYTES)


def delivery_budget(delivery: Any, envelope_bytes: int) -> None:
    delivery.retained_execution_bytes = envelope_bytes
    delivery.retained_metadata_bytes = DELIVERY_METADATA_BYTES
    validate_metadata(delivery, DELIVERY_METADATA_BYTES)
