"""Knowledge order, attachment correction and stop priority stay domain-owned."""

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from dlr.control.ai import tools as dlr_tools
from dlr.control.services.ai import _KnowledgeRetrievalState
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

from control_probe import Ledger, PolicyStop, agent_for, compatible_agent
from dispatch_model import DISPATCH_TOOL
from owned_work import OwnedTransport
from probe_support import FINAL_TEXT, PROTOCOLS, Protocol, model, response, server


@pytest.fixture
def knowledge_tools(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    started: list[str] = []
    results: dict[str, dict[str, Any]] = {
        "list_knowledge_bases": {"items": [{"id": "base-a", "source": "ima:v1:base-a"}]},
        "search_knowledge": {
            "items": [
                {
                    "id": "hit-a",
                    "title": "synthetic",
                    "summary": "evidence",
                    "source": "ima:v1:hit-a",
                }
            ],
            "returned_matches": 1,
            "is_end": True,
        },
    }
    for name in ("list_knowledge_bases", "search_knowledge", "read_knowledge"):

        def handler(args: dict[str, Any], tool_name: str = name) -> dict[str, Any]:
            started.append(tool_name)
            if tool_name == "read_knowledge":
                raise dlr_tools.ToolFailure("ks_full_text_unavailable", "synthetic failure")
            return results[tool_name]

        monkeypatch.setitem(
            dlr_tools._TOOLS, name, replace(dlr_tools._TOOLS[name], handler=handler)
        )
    return started


def knowledge_ledger() -> Ledger:
    return Ledger(
        knowledge=_KnowledgeRetrievalState.create(True, True),
        context=dlr_tools.ToolExecutionContext(knowledge_search_enabled=True),
    )


def listing() -> ToolCallPart:
    return ToolCallPart("list_knowledge_bases", {"source": "ima"}, "list")


def search() -> ToolCallPart:
    return ToolCallPart(
        "search_knowledge",
        {"source": "ima", "knowledge_base_id": "base-a", "query": "synthetic"},
        "search",
    )


def test_knowledge_premature_final_is_discarded_then_framework_drives_list_search(
    knowledge_tools: list[str],
) -> None:
    ledger = knowledge_ledger()
    invalid = '{"message":"ungrounded-output-must-not-be-resent","candidate":null}'
    result = agent_for(
        ledger,
        compatible=True,
        model_responses=[
            ModelResponse(parts=[TextPart(invalid)]),
            ModelResponse(parts=[listing()]),
            ModelResponse(parts=[search()]),
            ModelResponse(parts=[TextPart(FINAL_TEXT)]),
        ],
    ).run_sync("Synthetic", deps=ledger)
    assert result.output.message == "B0 answer"
    assert knowledge_tools == ["list_knowledge_bases", "search_knowledge"]
    assert ledger.knowledge.has_search_evidence
    assert ledger.state.total_tool_calls == 2
    assert ledger.state.tool_rounds == 2
    assert ledger.state.consecutive_failures == 0
    assert ledger.state.format_corrections == 0
    assert ledger.model_calls == 4
    assert "ungrounded-output-must-not-be-resent" not in str(result.all_messages())


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_real_sdk_knowledge_correction_omits_ungrounded_response_and_keeps_tool_schema(
    protocol: Protocol,
    knowledge_tools: list[str],
) -> None:
    ledger = knowledge_ledger()
    first = response(protocol, '{"message":"ungrounded-must-not-resend","candidate":null}')
    with server(
        first,
        response(protocol, calls=[("list_knowledge_bases", "list", json.dumps(listing().args))]),
        response(protocol, calls=[("search_knowledge", "search", json.dumps(search().args))]),
        response(protocol),
    ) as script:

        async def run() -> None:
            async with model(protocol, script.url, transport=OwnedTransport()) as sdk:
                assert (
                    await compatible_agent(sdk).run("Synthetic", deps=ledger)
                ).output.candidate is None

        asyncio.run(run())
        assert len(script.requests) == 4
        assert all(b"ungrounded-must-not-resend" not in request.body for request in script.requests)
        assert all(DISPATCH_TOOL.encode() not in request.body for request in script.requests)
    assert ledger.state.total_tool_calls == 2
    assert ledger.state.format_corrections == 0


def test_unknown_before_knowledge_list_consumes_failure_but_not_dispatch_budget(
    knowledge_tools: list[str],
) -> None:
    ledger = knowledge_ledger()
    result = agent_for(
        ledger,
        [ToolCallPart("unknown_probe", "{}", "unknown")],
        [listing()],
        [search()],
        compatible=True,
    ).run_sync("Synthetic", deps=ledger)
    assert result.output.candidate is None
    assert [execution.error_code for execution in ledger.dispatch_results] == [
        "ai_knowledge_sequence_invalid",
        None,
        None,
    ]
    assert ledger.state.total_tool_calls == 2
    assert ledger.state.tool_rounds == 3
    assert knowledge_tools == ["list_knowledge_bases", "search_knowledge"]


def test_failed_optional_full_text_read_preserves_search_evidence_and_stops_before_next_request(
    knowledge_tools: list[str],
) -> None:
    ledger = knowledge_ledger()
    with pytest.raises(PolicyStop, match="knowledge_read_failed"):
        agent_for(
            ledger,
            [listing()],
            [search()],
            [
                ToolCallPart("read_knowledge", {"source": "ima", "item_id": "hit-a"}, "read"),
            ],
            compatible=True,
        ).run_sync("Synthetic", deps=ledger)
    assert ledger.state.total_tool_calls == 3
    assert ledger.model_calls == 3
    assert ledger.knowledge.has_search_evidence
    assert ledger.knowledge.degraded_error_codes == {"ks_full_text_unavailable"}
    assert ledger.knowledge.full_text_sources == set()


def test_three_premature_knowledge_finals_stop_without_any_dispatch(
    knowledge_tools: list[str],
) -> None:
    ledger = knowledge_ledger()
    with pytest.raises(PolicyStop, match="knowledge_sequence_incomplete"):
        agent_for(ledger, compatible=True).run_sync("Synthetic", deps=ledger)
    assert ledger.model_calls == 3
    assert ledger.state.total_tool_calls == 0
    assert ledger.state.tool_rounds == 0
    assert knowledge_tools == []


def test_unread_attachment_gets_exactly_one_correction_then_stops() -> None:
    ledger = Ledger()
    ledger.state.attachment_read_required = True
    with pytest.raises(PolicyStop, match="attachment_content_unread"):
        agent_for(ledger, compatible=True).run_sync("Synthetic", deps=ledger)
    assert ledger.model_calls == 2
    assert ledger.state.attachment_read_correction
    assert ledger.state.total_tool_calls == 0


def test_failed_attachment_read_never_earns_another_correction() -> None:
    ledger = Ledger()
    ledger.state.attachment_read_required = True
    ledger.state.attachment_read_attempts = 1
    with pytest.raises(PolicyStop, match="attachment_content_unread"):
        agent_for(ledger, compatible=True).run_sync("Synthetic", deps=ledger)
    assert ledger.model_calls == 1
    assert not ledger.state.attachment_read_correction


@pytest.mark.parametrize(
    "result_size,expected", [(16001, "tool_result_budget"), (0, "consecutive_tool_failures")]
)
def test_result_bytes_take_priority_over_failures_and_call_limit(
    result_size: int, expected: str
) -> None:
    ledger = Ledger()
    ledger.state.total_tool_calls = 15
    ledger.state.consecutive_failures = 2
    execution = dlr_tools.rejected_tool_call("unknown", "ai_tool_unknown")
    ledger.state.record_execution(replace(execution, result_size=result_size))
    assert ledger.state.total_tool_calls == 16
    assert ledger.state.stop_reason == expected
