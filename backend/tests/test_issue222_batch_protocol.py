"""The bounded closed batch result is evidence only after exact validation."""

from copy import deepcopy

import pytest

from dlr.control.services.subscription_batch_protocol import (
    RESULT_SCHEMA,
    control_decision,
    digest,
    encode,
    validate_result,
)

MANIFEST_ASCII = (
    b'[{"content_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    b'"delivery_id":20,"delivery_ordinal":1,'
    b'"envelope_hash":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
    b'"member_index":0,"receipt_id":10},'
    b'{"content_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    b'"delivery_id":21,"delivery_ordinal":1,'
    b'"envelope_hash":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
    b'"member_index":1,"receipt_id":11}]'
)
MANIFEST_SHA256 = "e5a426966fa2879d8100f1e24823cc951a4c2294752e06211f0265277877b580"


def manifest(count=3):
    return [
        {
            "receipt_id": i + 10,
            "delivery_id": i + 20,
            "delivery_ordinal": 1,
            "member_index": i,
            "content_hash": "a" * 64,
            "envelope_hash": "b" * 64,
        }
        for i in range(count)
    ]


def result(items, statuses=None):
    statuses = statuses or [("succeeded", None)] * len(items)
    return {
        "schema": RESULT_SCHEMA,
        "batch_id": "f" * 32,
        "manifest_hash": digest(items),
        "members": [
            {
                key: item[key]
                for key in ("receipt_id", "delivery_id", "delivery_ordinal", "member_index")
            }
            | {"status": state, "error_code": code}
            for item, (state, code) in zip(items, statuses, strict=True)
        ],
    }


def validate(items, raw, normalized=None, **kwargs):
    return validate_result(
        "f" * 32,
        digest(items),
        items,
        raw,
        raw if normalized is None else normalized,
        worker_truncated=kwargs.get("worker_truncated", False),
        control_truncated=kwargs.get("control_truncated", False),
    )


def test_manifest_digest_matches_an_independent_fixed_ascii_vector():
    items = manifest(2)
    assert encode(items) == MANIFEST_ASCII
    assert digest(items) == MANIFEST_SHA256
    reordered_keys = [dict(reversed(list(item.items()))) for item in items]
    assert encode(reordered_keys) == MANIFEST_ASCII
    assert digest(reordered_keys) == MANIFEST_SHA256


@pytest.mark.parametrize("changed_field", ["content_hash", "envelope_hash", "member_order"])
def test_manifest_content_and_order_changes_reject_the_original_result_binding(changed_field):
    original = manifest(2)
    frozen_result = result(original)
    assert frozen_result["manifest_hash"] == MANIFEST_SHA256
    changed = deepcopy(original)
    if changed_field == "member_order":
        changed.reverse()
    else:
        changed[0][changed_field] = "c" * 64
    changed_hash = digest(changed)
    assert changed_hash != MANIFEST_SHA256
    decision = validate_result(
        "f" * 32,
        changed_hash,
        changed,
        frozen_result,
        frozen_result,
        worker_truncated=False,
        control_truncated=False,
    )
    assert decision.provenance == "control_unknown"
    assert decision.counts == {"succeeded": 0, "failed": 0, "unknown": 2, "not_run": 0}
    assert all(item["error_code"] == "batch_result_invalid" for item in decision.result["members"])


def test_success_and_partial_failure_keep_only_valid_member_facts():
    items = manifest()
    decision = validate(items, result(items))
    assert decision.execution_status == "succeeded"
    assert decision.counts == {"succeeded": 3, "failed": 0, "unknown": 0, "not_run": 0}
    partial = result(
        items,
        [
            ("succeeded", None),
            ("unknown", "target_commit_unknown"),
            ("not_run", "predecessor_failed"),
        ],
    )
    decision = validate(items, partial)
    assert decision.execution_status == "dead_letter"
    assert decision.provenance == "worker_validated"
    assert decision.counts == {"succeeded": 1, "failed": 0, "unknown": 1, "not_run": 1}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(schema="unrelated"),
        lambda value: value.update(batch_id="another"),
        lambda value: value.update(manifest_hash="bad"),
        lambda value: value.update(debug="secret"),
        lambda value: value["members"].pop(),
        lambda value: value["members"].reverse(),
        lambda value: value["members"][0].update(receipt_id=True),
        lambda value: value["members"][0].update(delivery_id=999),
        lambda value: value["members"][0].update(error_code="business_error"),
        lambda value: value["members"][0].update(status="not_run", error_code="predecessor_failed"),
        lambda value: value["members"][0].update(
            status="failed", error_code="unbounded error text"
        ),
        lambda value: value["members"][0].update(
            status="unknown", error_code="target_invalid_data"
        ),
        lambda value: value["members"][0].update(debug="x" * 100000),
        lambda value: value["members"][1].update(status="failed", error_code="business_error"),
    ],
)
def test_invalid_or_forged_output_never_counts_a_success(mutation):
    items = manifest()
    value = result(items)
    mutation(value)
    decision = validate(items, value)
    assert decision.provenance == "control_unknown"
    assert decision.counts["unknown"] == 3 and decision.counts["succeeded"] == 0
    assert all(item["error_code"] == "batch_result_invalid" for item in decision.result["members"])
    assert "debug" not in str(decision.result)


@pytest.mark.parametrize("flags", [{"worker_truncated": True}, {"control_truncated": True}])
def test_truncation_discards_even_apparent_success(flags):
    items = manifest()
    assert validate(items, result(items), **flags).counts["unknown"] == 3


def test_normalization_difference_and_control_not_started_are_conservative():
    items = manifest()
    raw = result(items)
    normalized = deepcopy(raw)
    normalized["members"][0]["receipt_id"] = float(raw["members"][0]["receipt_id"])
    assert validate(items, raw, normalized).counts["unknown"] == 3
    decision = control_decision("f" * 32, digest(items), items, not_started=True)
    assert decision.counts["not_run"] == 3 and decision.provenance == "control_not_started"


def test_deeply_nested_members_are_invalid_instead_of_interrupting_settlement():
    items = manifest()
    raw = result(items)
    nested = []
    for _ in range(1500):
        nested = [nested]
    raw["members"] = nested
    decision = validate(items, raw)
    assert decision.provenance == "control_unknown"
    assert decision.counts == {"succeeded": 0, "failed": 0, "unknown": 3, "not_run": 0}
    assert all(item["error_code"] == "batch_result_invalid" for item in decision.result["members"])
