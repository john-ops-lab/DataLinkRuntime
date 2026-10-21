"""Media/native Schema counters remain strict, non-content audit metadata."""

import pytest

from test_issue207_smoke_audit_contract import _contract


def record() -> dict[str, object]:
    contract = _contract()
    output: dict[str, object] = {key: 0 for key in contract["budget_counters"]}
    output.update(
        timestamp="2026-10-14T00:00:00Z",
        schema_version=1,
        event_type="provider_budget",
        request_id="request",
        conversation_id="conversation",
        adapter_id=1,
        provider_call_index=1,
        window_tokens=250,
        estimated_before_tokens=230,
        estimated_after_tokens=217,
        system_tokens=20,
        conversation_tokens=20,
        tool_message_tokens=5,
        tool_definition_tokens=5,
        output_reserve_tokens=100,
        safety_reserve_tokens=50,
        schema_tokens=17,
        payload_bytes=128,
        estimated_payload_bytes=192,
        max_payload_bytes=256,
        provider_deadline_ms=100,
        hard_deadline_ms=150,
        remaining_ms=90,
        purpose="assist_followup",
        window_source="verified_model",
        estimation_method="utf8_json_bytes_div2_plus_fixed_reserves_v1",
        status="prepared",
    )
    return output


def test_native_schema_and_wire_bytes_both_gate_prepared_status() -> None:
    contract = _contract()
    contract["validate_provider_audit"](record())
    rejected = record()
    rejected.update(payload_bytes=512, status="rejected")
    contract["validate_provider_audit"](rejected)


def test_estimated_bytes_can_reject_before_actual_bytes_exceed_limit() -> None:
    rejected = record()
    rejected.update(estimated_payload_bytes=512, status="rejected")
    _contract()["validate_provider_audit"](rejected)


@pytest.mark.parametrize(
    "field,value",
    [
        ("image_count", True),
        ("pdf_page_count", -1),
        ("media_bytes", float("nan")),
        ("schema_tokens", True),
        ("payload_bytes", False),
        ("max_payload_bytes", 0),
        ("payload_bytes", float("nan")),
        ("payload_bytes", -1),
        ("estimated_payload_bytes", False),
        ("estimated_payload_bytes", -1),
        ("estimated_payload_bytes", float("nan")),
        ("estimated_payload_bytes", 512),
        ("max_payload_bytes", 256.0),
        ("schema_tokens", -1),
        ("schema_tokens", 0),
        ("payload_bytes", 512),
        ("raw_media", "private-base64-body"),
    ],
)
def test_budget_audit_rejects_invalid_counter_or_content(field: str, value: object) -> None:
    output = record()
    output[field] = value
    with pytest.raises(AssertionError):
        _contract()["validate_provider_audit"](output)
