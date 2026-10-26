"""Closed business-batch wire protocol; no SQL, credentials or user error text."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

INPUT_SCHEMA = "dlr.event.batch.v1"
RESULT_SCHEMA = "dlr.event.batch.result.v1"
OUTCOME_SCHEMA = "dlr.event.batch.outcome.v1"
MAX_MEMBERS = 100
MAX_INPUT_BYTES = 256 * 1024
MAX_RESULT_BYTES = 64 * 1024
RESULT_HEADER_BYTES = 512
MEMBER_RESULT_BYTES = 512
BATCH_MEMBER_CODES = frozenset(
    {
        "business_error",
        "target_unavailable",
        "target_permission_denied",
        "target_identity_conflict",
        "target_invalid_data",
        "target_retry_exhausted",
        "target_commit_unknown",
        "execution_cancelled",
        "execution_timeout",
        "worker_lost",
        "result_unavailable",
        "batch_result_invalid",
        "batch_output_truncated",
        "predecessor_failed",
        "execution_not_started",
    }
)
_FAILED_CODES = frozenset(
    {
        "business_error",
        "target_unavailable",
        "target_permission_denied",
        "target_identity_conflict",
        "target_invalid_data",
        "target_retry_exhausted",
    }
)
_UNKNOWN_CODES = frozenset(
    {
        "target_commit_unknown",
        "execution_cancelled",
        "execution_timeout",
        "worker_lost",
        "result_unavailable",
    }
)
OUTCOMES = ("succeeded", "failed", "unknown", "not_run")
MEMBER_KEYS = frozenset(
    {"receipt_id", "delivery_id", "delivery_ordinal", "member_index", "status", "error_code"}
)


def encode(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def input_frame(
    batch_id: str, manifest_hash: str, subscription: dict[str, Any], members: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema": INPUT_SCHEMA,
        "batch_id": batch_id,
        "manifest_hash": manifest_hash,
        "subscription": subscription,
        "members": members,
    }


def singleton_reserve(envelope: dict[str, Any]) -> int:
    maximum = 9223372036854775807
    member = {
        "receipt_id": maximum,
        "delivery_id": maximum,
        "delivery_ordinal": maximum,
        "member_index": 99,
        "input": envelope,
    }
    frame = input_frame(
        "f" * 32,
        "f" * 64,
        {"id": str(maximum), "config_version": maximum, "source_epoch": "f" * 64},
        [member],
    )
    return len(encode(frame))


@dataclass(frozen=True)
class BatchFinishDecision:
    execution_status: str
    error_code: str | None
    result: dict[str, Any]
    output: dict[str, Any]
    provenance: str
    counts: dict[str, int]


def _decision(
    batch_id: str,
    manifest_hash: str,
    members: list[dict[str, Any]],
    provenance: str,
    output: dict[str, Any] | None = None,
) -> BatchFinishDecision:
    counts = {state: sum(item["status"] == state for item in members) for state in OUTCOMES}
    result = {
        "schema": OUTCOME_SCHEMA,
        "batch_id": batch_id,
        "manifest_hash": manifest_hash,
        "members": members,
    }
    complete = counts["succeeded"] == len(members)
    return BatchFinishDecision(
        "succeeded" if complete else "dead_letter",
        None if complete else "batch_member_failed",
        result,
        deepcopy(output if output is not None else result),
        provenance,
        counts,
    )


def control_decision(
    batch_id: str,
    manifest_hash: str,
    manifest: list[dict[str, Any]],
    code: str = "result_unavailable",
    *,
    not_started: bool = False,
) -> BatchFinishDecision:
    if code not in BATCH_MEMBER_CODES:
        code = "result_unavailable"
    members = [
        {
            key: item[key]
            for key in ("receipt_id", "delivery_id", "delivery_ordinal", "member_index")
        }
        | {
            "status": "not_run" if not_started else "unknown",
            "error_code": "execution_not_started" if not_started else code,
        }
        for item in manifest
    ]
    return _decision(
        batch_id,
        manifest_hash,
        members,
        "control_not_started" if not_started else "control_unknown",
    )


def validate_result(
    batch_id: str,
    manifest_hash: str,
    manifest: list[dict[str, Any]],
    raw: Any,
    normalized: Any,
    *,
    worker_truncated: bool,
    control_truncated: bool,
) -> BatchFinishDecision:
    bad_code = (
        "batch_output_truncated"
        if worker_truncated or control_truncated
        else "batch_result_invalid"
    )
    try:
        if worker_truncated or control_truncated or not 1 <= len(manifest) <= MAX_MEMBERS:
            raise ValueError
        limit = min(MAX_RESULT_BYTES, RESULT_HEADER_BYTES + MEMBER_RESULT_BYTES * len(manifest))
        for value in (raw, normalized):
            if not isinstance(value, dict) or set(value) != {
                "schema",
                "batch_id",
                "manifest_hash",
                "members",
            }:
                raise ValueError
            if (
                value["schema"] != RESULT_SCHEMA
                or value["batch_id"] != batch_id
                or value["manifest_hash"] != manifest_hash
                or len(encode(value)) > limit
            ):
                raise ValueError
            if not isinstance(value["members"], list) or len(value["members"]) != len(manifest):
                raise ValueError
            seen_failure = False
            for item, expected in zip(value["members"], manifest, strict=True):
                if not isinstance(item, dict) or set(item) != MEMBER_KEYS:
                    raise ValueError
                for key in ("receipt_id", "delivery_id", "delivery_ordinal", "member_index"):
                    if type(item[key]) is not int or item[key] != expected[key]:
                        raise ValueError
                state, code = item["status"], item["error_code"]
                if state == "succeeded":
                    if code is not None or seen_failure:
                        raise ValueError
                elif not seen_failure and state in {"failed", "unknown"}:
                    if not isinstance(code, str) or code not in (
                        _FAILED_CODES if state == "failed" else _UNKNOWN_CODES
                    ):
                        raise ValueError
                    seen_failure = True
                elif seen_failure and state == "not_run" and code == "predecessor_failed":
                    continue
                else:
                    raise ValueError
        if raw != normalized:
            raise ValueError
        return _decision(
            batch_id,
            manifest_hash,
            deepcopy(normalized["members"]),
            "worker_validated",
            deepcopy(normalized),
        )
    except (ValueError, TypeError, KeyError, OverflowError, UnicodeError, RecursionError):
        return control_decision(batch_id, manifest_hash, manifest, bad_code)
