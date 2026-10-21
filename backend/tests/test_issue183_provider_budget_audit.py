"""Each actual orchestration call has bounded counters and one shared deadline."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dlr.common.config import settings
from dlr.control.ai import context_budget, providers, tool_audit
from dlr.control.ai.runtime.pydantic_ai import control
from dlr.control.ai.runtime.pydantic_ai.messages import budget_view
from dlr.control.ai.runtime.pydantic_ai.transport import OwnedTransport
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
    received: list[float] = []
    retained_estimates: list[int] = []
    actual_bodies: list[bytes] = []
    actual_deadlines: list[float] = []
    configured_timeouts: list[dict[str, float]] = []
    private_marker = "PRIVATE_WORKING_COPY_AND_REFERENCE_BODY"

    original_prepare = control.prepare_messages

    def prepare(*args, **kwargs):
        prepared, result = original_prepare(*args, **kwargs)
        _, snapshot, tools, media = args
        view, _ = budget_view(prepared, media, kwargs.get("wire_view"))
        retained_estimates.append(
            context_budget.estimate_tokens(
                view, tools, purpose=kwargs["purpose"], adapter=snapshot.adapter
            )
        )
        return prepared, result

    original_before_request = control.DomainControl.before_model_request

    async def before_request(self, ctx, request_context):
        before = time.monotonic()
        result = await original_before_request(self, ctx, request_context)
        after = time.monotonic()
        run = ctx.deps
        state = run.execution.state
        configured_timeouts.append(
            {
                "before": before,
                "after": after,
                "timeout": result.model_settings["timeout"],
                "deadline": run.client.transport.hard_deadline,
                "expected_deadline": state.hard_deadline if run.finalizing else state.tool_deadline,
                "started_at": state.started_at,
                "hard_deadline": state.hard_deadline,
            }
        )
        return result

    original_transport = OwnedTransport.handle_async_request

    async def capture_body(self, request):
        actual_bodies.append(await request.aread())
        actual_deadlines.append(self.hard_deadline)
        return await original_transport(self, request)

    monkeypatch.setattr(control, "prepare_messages", prepare)
    monkeypatch.setattr(control.DomainControl, "before_model_request", before_request)
    monkeypatch.setattr(OwnedTransport, "handle_async_request", capture_body)

    def fake_request(_method, _url, _headers, payload=None, **kwargs):
        assert payload is not None
        received.append(kwargs["timeout_seconds"])
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
        assert (
            len(budgets)
            == len(results)
            == len(received)
            == len(actual_bodies)
            == len(retained_estimates)
            == len(configured_timeouts)
            == len(actual_deadlines)
            == (3 if stop_with_duplicate else 2)
        )
        assert [record["provider_call_index"] for record in budgets] == list(
            range(1, len(received) + 1)
        )
        assert len({record["hard_deadline_ms"] for record in budgets}) == 1
        assert budgets[0]["omitted_history_messages"] == 2
        assert all(budget["omitted_history_messages"] == 0 for budget in budgets[1:])
        assert budgets[0]["estimated_before_tokens"] > budgets[0]["estimated_after_tokens"]
        for budget, result, estimated, timeout, raw, configured, deadline in zip(
            budgets,
            results,
            retained_estimates,
            received,
            actual_bodies,
            configured_timeouts,
            actual_deadlines,
            strict=True,
        ):
            assert budget["estimated_after_tokens"] == estimated <= budget["window_tokens"]
            assert b" old-history " not in raw and b"old reply" not in raw
            assert private_marker.encode() in raw and str(body["message"]).encode() in raw
            assert budget["payload_bytes"] == len(raw) <= budget["max_payload_bytes"]
            assert budget["estimated_payload_bytes"] >= len(raw)
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
            assert deadline == configured["deadline"] == configured["expected_deadline"]
            assert timeout == configured["timeout"] > 0
            assert (
                min(settings.ai_provider_timeout_seconds, max(0, deadline - configured["after"]))
                <= timeout
                <= min(
                    settings.ai_provider_timeout_seconds, max(0, deadline - configured["before"])
                )
            )
            assert budget["provider_deadline_ms"] == int(
                (deadline - configured["started_at"]) * 1000
            )
            assert budget["hard_deadline_ms"] == int(
                (configured["hard_deadline"] - configured["started_at"]) * 1000
            )
            assert budget["elapsed_ms"] + budget["remaining_ms"] in (
                budget["provider_deadline_ms"],
                budget["provider_deadline_ms"] - 1,
            )
            assert budget["request_id"] == result["request_id"]
        assert budgets[-1]["purpose"] == (
            "assist_finalization" if stop_with_duplicate else "assist_followup"
        )
        if stop_with_duplicate:
            assert budgets[-1]["tool_definition_tokens"] == 0
            assert budgets[-1]["provider_deadline_ms"] == budgets[-1]["hard_deadline_ms"]
    finally:
        tool_audit.close_ai_tool_audit_logging()
