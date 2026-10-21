"""P5/P6: measure true lifetime; known gaps are explicit observation tests."""

import asyncio
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from dlr.control.ai import providers
from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.capabilities import UseThreadExecutor
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import RunContext

from guards import GuardedTransport, Rejected, cause_is
from probe_support import (
    FINAL_TEXT,
    PROTOCOLS,
    CapturedRequest,
    Final,
    Protocol,
    Reply,
    model,
    response,
    server,
)


async def wait_set(event: threading.Event, limit: float = 2) -> None:
    deadline = time.monotonic() + limit
    while not event.is_set():
        assert time.monotonic() < deadline, "fixture did not reach its synchronization point"
        await asyncio.sleep(0.005)


def test_cross_loop_clients_share_40_permits_with_existing_catalog_path() -> None:
    permits = providers._TRANSPORT_PERMITS
    borrowed = 0
    release = threading.Event()
    replies = [response(protocol) for protocol in PROTOCOLS]
    for reply in replies:
        reply.wait = release
    futures = []
    try:
        # 37 synthetic reservations + 3 real SDK transports exercise the
        # actual 40-permit boundary without a 40-connection load benchmark.
        for _ in range(37):
            assert permits.acquire(blocking=False)
            borrowed += 1

        def dispatch(request: CapturedRequest) -> Reply:
            if "chat/completions" in request.path:
                return replies[0]
            if "/messages" in request.path:
                return replies[1]
            return replies[2]

        with (
            server(*replies, responder=dispatch) as script,
            ThreadPoolExecutor(max_workers=3) as pool,
        ):

            def run(protocol: Protocol) -> None:
                async def request() -> None:
                    async with model(protocol, script.url, transport=GuardedTransport()) as sdk:
                        result = await Agent(sdk, output_type=PromptedOutput(Final), retries=0).run(
                            "Synthetic input"
                        )
                        assert result.output.message == "B0 answer"

                asyncio.run(request())

            try:
                futures = [pool.submit(run, protocol) for protocol in PROTOCOLS]
                for reply in replies:
                    assert reply.entered.wait(2)
                if permits.acquire(blocking=False):
                    permits.release()
                    pytest.fail("shared transport capacity was not held")

                async def forty_first() -> None:
                    async with model(
                        "openai",
                        script.url,
                        transport=GuardedTransport(capacity_timeout=0.03),
                    ) as sdk:
                        with pytest.raises(Exception) as caught:
                            await Agent(sdk, retries=0).run("Must not reach HTTP")
                        assert cause_is(caught.value, "capacity", "timeout")

                asyncio.run(forty_first())
                with pytest.raises(providers.AiProviderError) as caught:
                    providers._request_json(
                        "GET",
                        script.url + "/catalog",
                        {},
                        not_found_code="ai_model_not_found",
                        timeout_seconds=0.03,
                    )
                assert caught.value.code == "ai_timeout"
                assert len(script.requests) == 3
            finally:
                release.set()
            for future in futures:
                future.result(timeout=3)
    finally:
        release.set()
        for _ in range(borrowed):
            permits.release()
    held = 0
    try:
        while permits.acquire(blocking=False):
            held += 1
        assert held == 40
    finally:
        for _ in range(held):
            permits.release()


@pytest.mark.gap
def test_cancelled_dns_outlives_transport_and_thin_wrapper_releases_capacity_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    permits = threading.BoundedSemaphore(1)

    def slow_dns(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        release.wait(3)
        ended.set()
        raise socket.gaierror("synthetic DNS failure")

    monkeypatch.setattr(socket, "getaddrinfo", slow_dns)

    async def scenario() -> None:
        async with model(
            "openai",
            "http://dns.b0.invalid",
            transport=GuardedTransport(permits=permits),
        ) as sdk:
            task = asyncio.create_task(Agent(sdk, retries=0).run("Synthetic input"))
            try:
                await wait_set(entered)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not ended.is_set()
                # This passing assertion demonstrates P5 incompatibility:
                # physical DNS still runs, yet the transport's finally freed the slot.
                assert permits.acquire(blocking=False)
                permits.release()
            finally:
                release.set()

    asyncio.run(scenario())
    assert ended.wait(2)


@pytest.mark.gap
def test_cancelled_sync_tool_needs_an_owner_beyond_agent_and_request_finally() -> None:
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    pins: set[str] = set()
    replies = iter(
        [
            ModelResponse(parts=[ToolCallPart("read_page", {}, "b0-slow")]),
            ModelResponse(parts=[TextPart(FINAL_TEXT)]),
        ]
    )

    def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return next(replies)

    def read_page() -> dict[str, str]:
        pins.add("synthetic/page/1")
        entered.set()
        try:
            release.wait(3)
            return {"source": "synthetic/page/1"}
        finally:
            pins.remove("synthetic/page/1")
            ended.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        agent = Agent(
            FunctionModel(scripted),
            tools=[read_page],
            output_type=PromptedOutput(Final),
            retries=0,
            capabilities=[UseThreadExecutor(executor)],
        )

        async def scenario() -> None:
            task = asyncio.create_task(agent.run("Synthetic input"))
            try:
                await wait_set(entered)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not ended.is_set()
                assert pins == {"synthetic/page/1"}
            finally:
                release.set()

        asyncio.run(scenario())
        assert ended.wait(2)
    assert pins == set()


def test_two_requests_keep_context_separate_and_observe_revocation_before_read() -> None:
    entered, release = threading.Event(), threading.Event()
    allowed = {"user-a": True, "user-b": False}
    seen: list[tuple[str, str, str]] = []
    lock = threading.Lock()

    def read_scope(ctx: RunContext[tuple[str, str, str]]) -> str:
        user, adapter, scope = ctx.deps
        with lock:
            if not allowed[user]:
                raise Rejected("permission", "revoked")
            seen.append((user, adapter, scope))
        entered.set()
        assert release.wait(2)
        return scope

    def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if any(isinstance(message, ModelResponse) for message in messages):
            return ModelResponse(parts=[TextPart("synthetic done")])
        return ModelResponse(parts=[ToolCallPart("read_scope", {}, "b0-scope")])

    with ThreadPoolExecutor(max_workers=2) as pool:
        agent = Agent(
            FunctionModel(scripted),
            tools=[read_scope],
            deps_type=tuple,
            retries=0,
            capabilities=[UseThreadExecutor(pool)],
        )

        async def scenario() -> None:
            task_a = asyncio.create_task(
                agent.run("Synthetic input", deps=("user-a", "adapter-a", "scope-a"))
            )
            try:
                await wait_set(entered)
                with pytest.raises((Rejected, UnexpectedModelBehavior)) as caught:
                    await agent.run("Synthetic input", deps=("user-b", "adapter-b", "scope-b"))
                assert isinstance(caught.value, Rejected)
                assert seen == [("user-a", "adapter-a", "scope-a")]
            finally:
                release.set()
            result = await task_a
            assert result.output == "synthetic done"

        asyncio.run(scenario())
    # Synthetic ACL/context evidence only; no claim about DB transaction visibility.
