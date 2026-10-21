"""Actual SDK request counts and safe correction history on all three protocols."""

import asyncio

import pytest
from dlr.control.ai import tools as dlr_tools
from dlr.control.ai.diagnostics import AiResponseInvalid

from control_probe import Ledger
from final_probe import corrected_final, stopped_final
from owned_work import OwnedTransport
from probe_support import CANARY, PROTOCOLS, Protocol, model, response, server

INVALID = '{"message":"invalid-final-must-not-be-resent","message":"duplicate","candidate":null}'


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("second_valid", [True, False])
def test_one_tool_free_correction_shares_budget_and_never_resends_invalid_final(
    protocol: Protocol,
    second_valid: bool,
) -> None:
    ledger = Ledger()
    ledger.state.total_tool_calls = 15
    ledger.state.tool_rounds = 7
    deadline = ledger.state.hard_deadline
    second = response(protocol) if second_valid else response(protocol, INVALID)
    with server(response(protocol, INVALID), second) as script:

        async def run() -> None:
            async with model(protocol, script.url, transport=OwnedTransport()) as sdk:
                if second_valid:
                    result = await corrected_final(sdk, ledger, "Original synthetic request")
                    assert result.candidate is None
                else:
                    with pytest.raises(AiResponseInvalid):
                        await corrected_final(sdk, ledger, "Original synthetic request")

        asyncio.run(run())
        assert len(script.requests) == 2
        assert b"invalid-final-must-not-be-resent" not in script.requests[1].body
        assert b"Original synthetic request" in script.requests[1].body
        assert all(not request.json().get("tools") for request in script.requests)
    assert ledger.model_calls == 2
    assert ledger.state.format_corrections == 1
    assert ledger.state.total_tool_calls == 15
    assert ledger.state.tool_rounds == 7
    assert ledger.state.hard_deadline == deadline


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize(
    "text",
    ['{"message":"\\ud800","candidate":null}', '{"message":"' + CANARY + '","candidate":null}'],
)
def test_unicode_and_secret_safety_failures_never_get_format_correction(
    protocol: Protocol, text: str
) -> None:
    ledger = Ledger(api_key=CANARY)
    with server(response(protocol, text)) as script:

        async def run() -> None:
            async with model(protocol, script.url, transport=OwnedTransport()) as sdk:
                with pytest.raises(AiResponseInvalid) as caught:
                    await corrected_final(sdk, ledger, "Original synthetic request")
                assert caught.value.stage in {"unicode", "output_safety"}
                assert CANARY not in str(caught.value)

        asyncio.run(run())
        assert len(script.requests) == 1
    assert ledger.state.format_corrections == 0


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("valid", [True, False])
def test_stopped_run_gets_at_most_one_tool_free_finalization(
    protocol: Protocol, valid: bool
) -> None:
    ledger = Ledger()
    successful = dlr_tools.execute_tool_call("dlr_docs_list", "{}", None)
    assert successful.status == "success"
    ledger.dispatch_results.append(successful)
    ledger.state.record_execution(successful)
    ledger.state.stop_reason = "duplicate_tool_call"
    with server(response(protocol) if valid else response(protocol, INVALID)) as script:

        async def run() -> None:
            async with model(protocol, script.url, transport=OwnedTransport()) as sdk:
                result = await stopped_final(
                    sdk, ledger, "Use only sanitized recorded tool results"
                )
                assert (result is not None) is valid

        asyncio.run(run())
        assert len(script.requests) == 1
        assert not script.requests[0].json().get("tools")
    assert ledger.model_calls == 1
    assert ledger.state.format_corrections == 0
    assert ledger.state.total_tool_calls == 1


@pytest.mark.parametrize("no_evidence", [True, False])
def test_unread_or_no_evidence_stop_uses_no_provider_request(no_evidence: bool) -> None:
    ledger = Ledger()
    ledger.state.stop_reason = "tool_call_budget"
    if not no_evidence:
        ledger.dispatch_results.append(dlr_tools.execute_tool_call("dlr_docs_list", "{}", None))
        ledger.state.attachment_read_required = True
    with server() as script:

        async def run() -> None:
            async with model("openai", script.url, transport=OwnedTransport()) as sdk:
                assert await stopped_final(sdk, ledger, "Synthetic") is None

        asyncio.run(run())
        assert script.requests == []
