"""Strict response failures remain public-compatible and privately distinguishable."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dlr.common.config import settings
from dlr.control.ai import providers, tool_audit
from test_ai import assist_body, configure, create_adapter, fake_chat_response, valid_output


@pytest.mark.parametrize(
    ("case", "stage", "reason"),
    [
        ("envelope", "provider_envelope", "invalid_shape"),
        ("incomplete", "provider_envelope", "incomplete_completion"),
        ("json", "final_json", "malformed_json"),
        ("duplicate", "final_json", "duplicate_key"),
        ("constant", "final_json", "non_finite_number"),
        ("schema", "output_schema", "schema_mismatch"),
        ("unicode", "unicode", "invalid_unicode"),
        ("secret", "output_safety", "secret_reflection"),
        ("requirements", "candidate_configuration", "requirements_mismatch"),
        ("runtime_config", "candidate_configuration", "runtime_config_mismatch"),
    ],
)
def test_invalid_response_has_fixed_failure_layer_without_sensitive_content(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
    stage: str,
    reason: str,
) -> None:
    tool_audit.close_ai_tool_audit_logging()
    monkeypatch.setattr(settings, "platform_log_root", str(tmp_path))
    adapter = create_adapter(api_client, f"issue180-{case}")
    configure(api_client)
    marker = "PRIVATE_PROMPT_CODE_SECRET_NAME_AND_VALUE_DO_NOT_LOG"
    output = valid_output(marker)
    response: object = fake_chat_response(output)
    if case == "envelope":
        response = {"private-marker": marker}
    elif case == "incomplete":
        response["choices"][0]["finish_reason"] = "length"
    elif case == "json":
        response = fake_chat_response('{"message":' + marker)
    elif case == "duplicate":
        response = fake_chat_response('{"message":"' + marker + '","message":"x","candidate":null}')
    elif case == "constant":
        response = fake_chat_response('{"message":"' + marker + '","candidate":NaN}')
    elif case == "schema":
        output["unexpected-secret-name"] = marker
        response = fake_chat_response(output)
    elif case == "unicode":
        response = fake_chat_response('{"message":"\\ud800","candidate":null}')
    elif case == "secret":
        from dlr.control.services import ai as ai_service

        monkeypatch.setattr(ai_service, "_resolve_api_key", lambda *args: marker)
    elif case in {"requirements", "runtime_config"}:
        output["candidate"][case] = marker if case == "requirements" else {marker: marker}
        response = fake_chat_response(output)
    monkeypatch.setattr(providers, "_request_json", lambda *args, **kwargs: response)
    try:
        result = api_client.post(
            f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body(code=marker)
        )
        assert result.status_code == 502
        assert result.json()["detail"]["code"] == "ai_response_invalid"
        serialized = tool_audit.audit_log_path().read_text()
        assert marker not in serialized and "unexpected-secret-name" not in serialized
        records = [json.loads(line) for line in serialized.splitlines()]
        failures = [record for record in records if record["event_type"] == "response_validation"]
        assert len(failures) == 1
        assert failures[0]["stage"] == stage and failures[0]["reason"] == reason
        assert failures[0]["provider_call_index"] == 1
        assert failures[0]["request_id"] == records[-1]["request_id"]
    finally:
        tool_audit.close_ai_tool_audit_logging()


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"\xff-private-wire-response", "invalid_utf8"),
        (b'{"private-wire-response":', "malformed_json"),
        (b'{"private-wire-response":1,"private-wire-response":2}', "duplicate_key"),
        (b'{"private-wire-response":NaN}', "non_finite_number"),
        (
            b"[" * 30000 + b"0" + b"]" * 30000,
            "json_limit",
        ),
        (b"9" * 5000, "json_value_invalid"),
    ],
    ids=["utf8", "malformed", "duplicate", "non-finite", "nesting-limit", "numeric-limit"],
)
def test_actual_wire_decode_keeps_fixed_reason_and_discards_raw_response(
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
    reason: str,
) -> None:
    from io import BytesIO

    monkeypatch.setattr(providers._NO_REDIRECT_OPENER, "open", lambda *args, **kwargs: BytesIO(raw))
    with pytest.raises(providers.AiProviderError) as caught:
        providers._request_json(
            "POST", "https://provider.invalid", {}, not_found_code="ai_model_not_found"
        )
    assert caught.value.code == "ai_response_invalid"
    assert caught.value.stage == "provider_json" and caught.value.reason == reason
    assert "private-wire-response" not in str(caught.value)
