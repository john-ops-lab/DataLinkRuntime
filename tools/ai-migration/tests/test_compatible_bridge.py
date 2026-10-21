"""New compatibility assertions supplement, rather than replace, first gap evidence."""

import asyncio
import socket
import threading
import time
from typing import Any

import pytest
from dlr.control.ai import tools as dlr_tools
from pydantic_ai import Agent, PromptedOutput, Tool
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.tools import RunContext, ToolDefinition

from control_probe import SCHEMA, Ledger, SerialBudget, agent_for, dispatch
from dispatch_model import DISPATCH_TOOL, DispatchModel
from owned_work import OwnedTransport, WorkOwner
from probe_support import PROTOCOLS, Final, Protocol, Reply, model, response, server


@pytest.fixture
def starts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    started: list[str] = []

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        started.append(args["value"])
        return args

    monkeypatch.setitem(
        dlr_tools._TOOLS,
        "probe_read",
        dlr_tools.ToolSpec("probe_read", "Synthetic", SCHEMA, handler),
    )
    return started


def test_unknown_then_valid_tool_preserves_budget_and_framework_continuation(
    starts: list[str],
) -> None:
    ledger = Ledger()
    result = agent_for(
        ledger,
        [ToolCallPart("unknown_probe", "{}", "unknown")],
        [ToolCallPart("probe_read", '{"value":"ok"}', "known")],
        compatible=True,
    ).run_sync("Synthetic", deps=ledger)
    assert result.output.message == "B0 answer"
    assert ledger.state.tool_rounds == 2
    assert ledger.state.total_tool_calls == 2
    assert ledger.state.consecutive_failures == 0
    assert [execution.error_code for execution in ledger.dispatch_results] == [
        "ai_tool_unknown",
        None,
    ]
    assert ledger.model_calls == 3
    assert starts == ["ok"]
    wire = DispatchModel.wire_messages(result.all_messages())
    calls = [
        p
        for m in wire
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, ToolCallPart)
    ]
    results = [
        p
        for m in wire
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    ]
    assert [(p.tool_name, p.tool_call_id, p.args) for p in calls] == [
        ("unknown_probe", "unknown", "{}"),
        ("probe_read", "known", '{"value":"ok"}'),
    ]
    assert [(p.tool_name, p.tool_call_id) for p in results] == [
        ("unknown_probe", "unknown"),
        ("probe_read", "known"),
    ]


@pytest.mark.parametrize(
    "raw", ['{"value":"x","value":"y"}', '{"value":1}', "not json", '{"value":"\\ud800"}']
)
def test_raw_arguments_reach_strict_dlr_validation_without_coercion(
    starts: list[str], raw: str
) -> None:
    ledger = Ledger()
    agent_for(ledger, [ToolCallPart("probe_read", raw, "bad")], compatible=True).run_sync(
        "Synthetic", deps=ledger
    )
    assert ledger.state.total_tool_calls == 1
    assert ledger.dispatch_results[0].error_code == "ai_tool_args_invalid"
    assert starts == []
    assert ledger.model_calls == 2


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_real_sdk_sees_only_original_tools_and_preserves_unknown_call_result(
    starts: list[str], protocol: Protocol
) -> None:
    ledger = Ledger()

    def bridge(ctx: RunContext[Ledger], name: str, arguments: str) -> dict[str, Any]:
        return dispatch(ctx.deps, name, arguments)

    with server(
        response(protocol, calls=[("unknown_probe", "unknown", "{}")]),
        response(protocol),
    ) as script:

        async def run() -> None:
            async with model(protocol, script.url, transport=OwnedTransport()) as sdk:
                agent = Agent(
                    DispatchModel(
                        sdk, [ToolDefinition(name="probe_read", parameters_json_schema=SCHEMA)]
                    ),
                    deps_type=Ledger,
                    tools=[Tool(bridge, name=DISPATCH_TOOL, takes_ctx=True)],
                    capabilities=[SerialBudget()],
                    output_type=PromptedOutput(Final),
                    retries=0,
                )
                assert (await agent.run("Synthetic", deps=ledger)).output.message == "B0 answer"

        asyncio.run(run())
        assert len(script.requests) == 2
        assert all(DISPATCH_TOOL.encode() not in request.body for request in script.requests)
        assert b"unknown_probe" in script.requests[1].body
        assert b"ai_tool_unknown" in script.requests[1].body
    assert ledger.state.total_tool_calls == 1
    assert starts == []


def test_cancelled_dns_keeps_capacity_and_transport_until_real_work_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    permits = threading.BoundedSemaphore(1)
    owner = WorkOwner(1)
    transport = OwnedTransport(permits=permits, owner=owner)

    def slow_dns(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        release.wait(3)
        ended.set()
        raise socket.gaierror("synthetic DNS failure")

    monkeypatch.setattr(socket, "getaddrinfo", slow_dns)

    async def scenario() -> None:
        async with model("openai", "http://dns.b0.invalid", transport=transport) as sdk:
            task = asyncio.create_task(Agent(sdk, retries=0).run("Synthetic"))
            deadline = time.monotonic() + 2
            while not entered.is_set():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not permits.acquire(blocking=False)
        assert not transport.closed.is_set()

    try:
        started = time.monotonic()
        asyncio.run(scenario())
        assert time.monotonic() - started < 2
        assert not ended.is_set()
        assert not transport.closed.is_set()
        assert not permits.acquire(blocking=False)
        release.set()
        assert transport.closed.wait(2)
        assert ended.is_set()
        assert permits.acquire(timeout=2)
        permits.release()
    finally:
        release.set()
        owner.close()


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("status", [401, 503])
def test_non_json_http_errors_keep_status_precedence_without_hidden_retries(
    protocol: Protocol,
    status: int,
) -> None:
    with server(Reply(b"synthetic gateway failure", status)) as script:

        async def run() -> None:
            async with model(protocol, script.url, transport=OwnedTransport()) as sdk:
                with pytest.raises(ModelHTTPError) as caught:
                    await Agent(sdk, retries=0).run("Synthetic")
                assert caught.value.status_code == status

        asyncio.run(run())
        assert len(script.requests) == 1
