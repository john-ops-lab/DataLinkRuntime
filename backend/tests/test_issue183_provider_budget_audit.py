"""Each actual orchestration call has bounded counters and one shared deadline."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dlr.common.config import settings
from dlr.control.ai import context_budget, providers, tool_audit
from test_ai import assist_body, configure, create_adapter, fake_chat_response, valid_output


@pytest.mark.parametrize("stop_with_duplicate", [False, True])
def test_each_provider_call_audits_budget_trim_and_shared_deadline(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stop_with_duplicate: bool,
) -> None:
    tool_audit.close_ai_tool_audit_logging()
    monkeypatch.setattr(settings, "platform_log_root", str(tmp_path))
    adapter = create_adapter(api_client, f"issue183-{stop_with_duplicate}")
    configure(api_client)
    received: list[tuple[int, float]] = []
    private_marker = "PRIVATE_WORKING_COPY_AND_REFERENCE_BODY"

    def fake_request(_method, _url, _headers, payload=None, **kwargs):
        assert payload is not None
        received.append(
            (
                context_budget.estimate_tokens(payload["messages"], payload.get("tools")),
                kwargs["timeout_seconds"],
            )
        )
        if len(received) == 1 or (stop_with_duplicate and len(received) == 2):
            return {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": f"call-{len(received)}",
                                    "type": "function",
                                    "function": {"name": "dlr_docs_list", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ]
            }
        return fake_chat_response(valid_output(private_marker))

    monkeypatch.setattr(providers, "_request_json", fake_request)
    body = assist_body(code=private_marker)
    body["recent_messages"] = [
        {"role": "user", "content": (private_marker + " old-history ") * 900},
        {"role": "assistant", "content": "old reply"},
    ]
    try:
        response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
        assert response.status_code == 200, response.text
        serialized = tool_audit.audit_log_path().read_text()
        assert private_marker not in serialized
        records = [json.loads(line) for line in serialized.splitlines()]
        budgets = [record for record in records if record["event_type"] == "provider_budget"]
        results = [record for record in records if record["event_type"] == "provider_result"]
        assert len(budgets) == len(results) == len(received) == (3 if stop_with_duplicate else 2)
        assert [record["provider_call_index"] for record in budgets] == list(
            range(1, len(received) + 1)
        )
        assert len({record["hard_deadline_ms"] for record in budgets}) == 1
        assert budgets[0]["omitted_history_messages"] == 2
        assert budgets[0]["estimated_before_tokens"] > budgets[0]["estimated_after_tokens"]
        for budget, result, (estimated, timeout) in zip(budgets, results, received, strict=True):
            assert budget["estimated_after_tokens"] == estimated <= budget["window_tokens"]
            assert budget["estimated_after_tokens"] == sum(
                budget[name]
                for name in [
                    "system_tokens",
                    "conversation_tokens",
                    "tool_message_tokens",
                    "tool_definition_tokens",
                    "output_reserve_tokens",
                    "safety_reserve_tokens",
                ]
            )
            assert budget["remaining_ms"] >= 0 and result["duration_ms"] >= 0
            assert 0 < timeout * 1000 <= budget["remaining_ms"] + 1
            assert budget["request_id"] == result["request_id"]
        assert budgets[-1]["purpose"] == (
            "assist_finalization" if stop_with_duplicate else "assist_followup"
        )
        if stop_with_duplicate:
            assert budgets[-1]["tool_definition_tokens"] == 0
            assert budgets[-1]["provider_deadline_ms"] == budgets[-1]["hard_deadline_ms"]
    finally:
        tool_audit.close_ai_tool_audit_logging()
