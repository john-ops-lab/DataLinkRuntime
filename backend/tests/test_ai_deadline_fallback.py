"""The Assist return guard distinguishes server fallback from late provider output."""

import asyncio

import pytest
from fastapi import HTTPException

import ai_sdk_fixtures
from dlr.control.ai import providers, tool_audit
from dlr.control.ai.runtime.contracts import RuntimeOutcome
from dlr.control.ai.runtime.pydantic_ai.runtime import PydanticAiRuntime
from dlr.control.schemas.ai import AiAssistRequest, AiAssistResponse
from dlr.control.services import ai
from test_ai import assist_body, configure, create_adapter


def test_permission_recheck_cannot_admit_candidate_after_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(ai.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tool_audit, "_emit_record", lambda _record: None)
    monkeypatch.setattr(
        PydanticAiRuntime,
        "execute",
        lambda *args, **kwargs: RuntimeOutcome.from_response(
            AiAssistResponse(
                message="Prepared candidate",
                candidate={"summary": "update", "code": "return input", "required_secret_keys": []},
                provider="custom_openai_compatible",
                model="deadline-model",
            )
        ),
    )

    ai_sdk_fixtures.stub_preparation(monkeypatch)

    def recheck():
        clock[0] = 150.0

    with pytest.raises(HTTPException) as caught:
        ai.assist(None, 1, AiAssistRequest(**assist_body()), permission_check=recheck)
    assert caught.value.detail["code"] == "ai_timeout"


def test_cancelled_request_records_one_truthful_terminal(monkeypatch):
    events = []
    monkeypatch.setattr(tool_audit, "_emit_record", events.append)

    def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    ai_sdk_fixtures.stub_preparation(monkeypatch)
    monkeypatch.setattr(PydanticAiRuntime, "execute", cancelled)
    with pytest.raises(asyncio.CancelledError):
        ai.assist(None, 1, AiAssistRequest(**assist_body()))
    terminal = [event for event in events if event["event_type"] == "request_terminal"]
    assert len(terminal) == 1
    assert terminal[0]["status"] == "error"
    assert terminal[0]["error_code"] == "ai_assist_cancelled"


def test_final_provider_deadline_keeps_server_summary_through_api(api_client, monkeypatch):
    adapter = create_adapter(api_client, "deadline-safe-summary")
    configure(api_client)
    clock = [0.0]
    sent = []
    monkeypatch.setattr(ai.time, "monotonic", lambda: clock[0])

    def upstream(*args, **kwargs):
        sent.append(args[3])
        if len(sent) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "docs-1",
                                    "type": "function",
                                    "function": {"name": "dlr_docs_list", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        clock[0] = 120.0 if len(sent) == 2 else 150.0
        raise providers.AiProviderError("ai_timeout")

    monkeypatch.setattr(providers, "_request_json", upstream)
    body = assist_body()
    body["task_intent"] = "code_change"
    response = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["candidate"] is None and result["task_status"] == "incomplete"
    assert "已保留 1 个成功结果" in result["message"]
    assert [item["status"] for item in result["tool_calls"]] == ["success"]
    assert len(sent) == 3 and "tools" not in sent[-1]
    assert clock[0] == 150.0
    assert "server_fallback_returned" not in result


@pytest.mark.parametrize("has_candidate", [False, True])
def test_stop_reason_and_candidate_null_do_not_admit_late_provider(monkeypatch, has_candidate):
    clock = [0.0]
    monkeypatch.setattr(ai.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tool_audit, "_emit_record", lambda _record: None)

    def late_provider_result(self, prepared, resources):
        clock[0] = 150.0
        resources.execution.state.stop_reason = ai._STOP_DEADLINE
        return RuntimeOutcome.from_response(
            AiAssistResponse(
                message="Late provider answer",
                candidate=(
                    {"summary": "late", "code": "return input", "required_secret_keys": []}
                    if has_candidate
                    else None
                ),
                provider="custom_openai_compatible",
                model="review-model",
            )
        )

    ai_sdk_fixtures.stub_preparation(monkeypatch)
    monkeypatch.setattr(PydanticAiRuntime, "execute", late_provider_result)
    with pytest.raises(HTTPException) as error:
        ai.assist(None, 1, AiAssistRequest(**assist_body()))
    assert error.value.status_code == 504 and error.value.detail["code"] == "ai_timeout"
