"""Real official SDK requests against a synthetic loopback HTTP service."""

import asyncio
import base64
import json
import threading
import time
from dataclasses import replace

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import BinaryContent, ModelRequest, UserPromptPart

from ai_runtime_support import FINAL_TEXT, Protocol, Reply, response, server
from dlr.control.ai.provider_config import (
    PROVIDERS,
    AiProviderError,
    ProviderSnapshot,
)
from dlr.control.ai.runtime.pydantic_ai.models import model_client, output_type, safe_error
from dlr.control.ai.runtime.pydantic_ai.strict_output import StrictOutput
from dlr.control.ai.runtime.pydantic_ai.transport import OwnedTransport
from dlr.control.schemas.ai import AiSettingDraft

CANARY = "sdk-contract-secret-canary"
PROTOCOLS: tuple[Protocol, ...] = ("openai", "anthropic", "google")


def snapshot(
    protocol: Protocol, url: str, mode: str = "prompt_only", *, key: str | None = CANARY
) -> ProviderSnapshot:
    provider = (
        "openai" if protocol == "openai" else "anthropic" if protocol == "anthropic" else "gemini"
    )
    adapter = replace(PROVIDERS[provider], structured_output=mode)  # type: ignore[arg-type]
    return ProviderSnapshot.create(
        AiSettingDraft(
            provider=provider,
            base_url=url,
            model="contract-model",
        ),
        adapter,
        key,
    )


async def run(value: ProviderSnapshot, *, media: bool = False) -> str:
    async with model_client(value, time.monotonic() + 3) as client:
        agent = Agent(
            client.model,
            output_type=output_type(value),
            retries=0,
            capabilities=[StrictOutput(value.api_key)],
        )
        agent.instrument = False
        history = (
            [
                ModelRequest(
                    parts=[
                        UserPromptPart(
                            [
                                "Return JSON matching the supplied output schema.",
                                BinaryContent(
                                    base64.b64decode(
                                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
                                    ),
                                    media_type="image/png",
                                ),
                            ]
                        )
                    ]
                )
            ]
            if media
            else None
        )
        result = await agent.run(
            None if media else "Return one JSON object.",
            message_history=history,
            model_settings=client.settings,
        )
        return result.output.message


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("mode", ["json_schema", "json_object", "prompt_only"])
def test_real_sdk_url_auth_and_output_without_extra_tool(protocol: Protocol, mode: str) -> None:
    with server(response(protocol)) as script:
        value = snapshot(protocol, script.url + "/gateway/v1", mode)
        assert asyncio.run(run(value)) == "Contract answer"
        assert len(script.requests) == 1
        request = script.requests[0]
        expected = (
            "/gateway/v1/chat/completions"
            if protocol == "openai"
            else "/gateway/v1/messages"
            if protocol == "anthropic"
            else "/gateway/v1beta/models/contract-model:generateContent"
        )
        # DLR normalizes the optional /v1 suffix for all custom protocols.
        if protocol == "anthropic":
            expected = "/gateway/v1/messages"
        assert request.path == expected
        header = (
            "authorization"
            if protocol == "openai"
            else "x-api-key"
            if protocol == "anthropic"
            else "x-goog-api-key"
        )
        assert CANARY in request.headers[header]
        body = request.json()
        assert not body.get("tools")
        encoded = request.body.decode()
        assert "final_result" not in encoded
        if protocol == "openai":
            assert body.get("response_format", {}).get("type") == (
                None if mode == "prompt_only" else mode
            )


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_anonymous_configuration_never_uses_environment_credentials(
    protocol: Protocol, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(name, "must-not-reach-wire")
    with server(response(protocol)) as script:
        assert asyncio.run(run(snapshot(protocol, script.url, key=None))) == "Contract answer"
        headers = script.requests[0].headers
        assert not any(
            headers.get(name) for name in ("authorization", "x-api-key", "x-goog-api-key")
        )
        assert "must-not-reach-wire" not in str(script.requests)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize(
    "status,code",
    [
        (401, "ai_auth_failed"),
        (404, "ai_model_not_found"),
        (429, "ai_provider_http_error"),
        (503, "ai_provider_http_error"),
    ],
)
def test_no_hidden_http_retry_and_stable_error(protocol: Protocol, status: int, code: str) -> None:
    with server(Reply(b"untrusted secret gateway error", status)) as script:
        value = snapshot(protocol, script.url)
        with pytest.raises(Exception) as caught:
            asyncio.run(run(value))
        mapped = safe_error(caught.value, value)
        assert mapped.code == code
        assert mapped.http_status == status
        assert len(script.requests) == 1
        assert "untrusted" not in str(mapped)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_http_json_guard_precedes_sdk_parser(protocol: Protocol) -> None:
    with server(Reply(b'{"choices":[],"choices":[]}')) as script:
        value = snapshot(protocol, script.url)
        with pytest.raises(Exception) as caught:
            asyncio.run(run(value))
        mapped = safe_error(caught.value, value)
        assert (mapped.stage, mapped.reason) == ("provider_json", "duplicate_key")
        assert len(script.requests) == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_media_is_encoded_by_real_sdk(protocol: Protocol) -> None:
    with server(response(protocol)) as script:
        asyncio.run(run(snapshot(protocol, script.url), media=True))
        encoded = script.requests[0].body
        assert b"iVBORw0KGgoAAAANSUhEUg" in encoded
        assert b"image/png" in encoded


def test_cancelled_loop_keeps_real_transport_permit_until_physical_body_end() -> None:
    wait = threading.Event()
    reply = response("openai")
    reply.wait = wait
    permits = threading.BoundedSemaphore(1)
    with server(reply) as script:
        transport = OwnedTransport(
            protocol="openai_compatible",
            authenticated=False,
            hard_deadline=time.monotonic() + 3,
            permits=permits,
        )

        async def cancel() -> None:
            import httpx2

            async with httpx2.AsyncClient(transport=transport) as client:
                task = asyncio.create_task(client.post(script.url, json={}))
                while not reply.entered.is_set():
                    await asyncio.sleep(0.001)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        asyncio.run(cancel())
        assert not permits.acquire(blocking=False)
        assert not transport.closed.is_set()
        wait.set()
        assert transport.closed.wait(2)
        assert permits.acquire(blocking=False)
        permits.release()


def test_raw_request_and_response_bounds() -> None:
    from dlr.control.ai.runtime.pydantic_ai.transport import inspect_envelope

    with pytest.raises(AiProviderError) as caught:
        inspect_envelope(b"NaN", "openai_compatible")
    assert caught.value.reason == "non_finite_number"
    assert json.loads(FINAL_TEXT)["candidate"] is None


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize(
    "text,stage,reason",
    [
        ('{"message":"a","message":"b","candidate":null}', "final_json", "duplicate_key"),
        ('{"message":3,"candidate":null}', "output_schema", "schema_mismatch"),
        (
            '{"message":"sdk-contract-secret-canary","candidate":null}',
            "output_safety",
            "secret_reflection",
        ),
    ],
)
def test_raw_final_guard_blocks_coercion_and_secret_reflection(
    protocol: Protocol, text: str, stage: str, reason: str
) -> None:
    from dlr.control.ai.diagnostics import AiResponseInvalid

    with server(response(protocol, text)) as script:
        with pytest.raises(AiResponseInvalid) as caught:
            asyncio.run(run(snapshot(protocol, script.url)))
        assert (caught.value.stage, caught.value.reason) == (stage, reason)
        assert len(script.requests) == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_real_sdk_tool_continuation_preserves_call_and_private_reasoning(
    protocol: Protocol,
) -> None:
    from pydantic_ai import Tool

    first = response(protocol, calls=[("read_contract", "call-contract", '{"value":"a"}')])
    if protocol == "anthropic":
        raw = json.loads(first.body)
        raw["content"].insert(
            0,
            {
                "type": "thinking",
                "thinking": "private-reasoning-canary",
                "signature": "signed-contract",
            },
        )
        first.body = json.dumps(raw).encode()

    def read_contract(value: str) -> str:
        return "read:" + value

    async def execute(value: ProviderSnapshot) -> str:
        async with model_client(value, time.monotonic() + 3) as client:
            agent = Agent(
                client.model,
                tools=[Tool(read_contract)],
                output_type=output_type(value),
                retries=0,
                capabilities=[StrictOutput(value.api_key)],
            )
            agent.instrument = False
            result = await agent.run(
                "Read the contract, then return JSON.", model_settings=client.settings
            )
            return result.output.message

    with server(first, response(protocol)) as script:
        output = asyncio.run(execute(snapshot(protocol, script.url)))
        assert output == "Contract answer"
        assert len(script.requests) == 2
        second = script.requests[1].body
        assert b"read:a" in second
        assert b"call-contract" in second
        if protocol == "anthropic":
            assert b"signed-contract" in second
            assert b"private-reasoning-canary" in second
            assert "private-reasoning-canary" not in output


def test_process_capacity_41st_sdk_request_does_not_start_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dlr.control.ai import providers

    permits = threading.BoundedSemaphore(40)
    monkeypatch.setattr(providers, "_TRANSPORT_PERMITS", permits)
    for _ in range(40):
        assert permits.acquire(blocking=False)

    async def execute(value: ProviderSnapshot) -> None:
        async with model_client(value, time.monotonic() + 0.05) as client:
            agent = Agent(client.model, output_type=output_type(value), retries=0)
            await agent.run("Return JSON.", model_settings=client.settings)

    with server(response("openai")) as script:
        value = snapshot("openai", script.url)
        try:
            with pytest.raises(Exception) as caught:
                asyncio.run(execute(value))
            assert safe_error(caught.value, value).code == "ai_timeout"
            assert not script.requests
        finally:
            for _ in range(40):
                permits.release()


@pytest.mark.parametrize(
    "provider", [name for name in PROVIDERS if name not in {"anthropic", "gemini"}]
)
def test_every_openai_profile_keeps_default_reasoning_and_versioned_gateway(provider: str) -> None:
    with server(response("openai")) as script:
        adapter = PROVIDERS[provider]  # type: ignore[index]
        value = ProviderSnapshot.create(
            AiSettingDraft(
                provider=provider, base_url=script.url + "/proxy/v1/v1/", model="profile-model"
            ),  # type: ignore[arg-type]
            adapter,
            CANARY,
        )
        assert asyncio.run(run(value)) == "Contract answer"
        request = script.requests[0]
        assert request.path == "/proxy/v1/chat/completions"
        body = request.json()
        assert body["model"] == "profile-model"
        assert "thinking" not in body
        assert "reasoning_effort" not in body
        if provider == "minimax":
            assert body["reasoning_split"] is True


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_redirect_not_followed(protocol: Protocol) -> None:
    with server(Reply(b"redirect", 307, {"Location": "/credential-trap"})) as script:
        with pytest.raises(AiProviderError) as caught:
            asyncio.run(run(snapshot(protocol, script.url)))
        assert caught.value.http_status == 307
        assert len(script.requests) == 1


@pytest.mark.parametrize(
    "request_limit,response_limit,code",
    [(2, 10000, "ai_context_over_budget"), (10000, 2, "ai_response_invalid")],
)
def test_actual_wire_bounds(request_limit: int, response_limit: int, code: str) -> None:
    import httpx2

    with server(response("openai")) as script:

        async def execute() -> None:
            transport = OwnedTransport(
                protocol="openai_compatible",
                authenticated=False,
                hard_deadline=time.monotonic() + 2,
                request_limit=request_limit,
                response_limit=response_limit,
            )
            async with httpx2.AsyncClient(transport=transport) as client:
                await client.post(script.url, content=b"{}x")

        with pytest.raises(AiProviderError) as caught:
            asyncio.run(execute())
        assert caught.value.code == code
        assert len(script.requests) == (0 if request_limit == 2 else 1)


def test_forty_physical_sdk_requests_share_capacity_with_catalog_beyond_loop_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.request import Request

    from dlr.control.ai import providers
    from dlr.control.ai.runtime.pydantic_ai.work import NETWORK_WORK

    permits = threading.BoundedSemaphore(40)
    monkeypatch.setattr(providers, "_TRANSPORT_PERMITS", permits)
    release = threading.Event()
    replies = [response("openai") for _ in range(40)]
    for reply in replies:
        reply.wait = release
    clients = []
    with server(*replies) as script:

        async def occupy() -> None:
            async with model_client(
                snapshot("openai", script.url), time.monotonic() + 10
            ) as client:
                clients.append(client)
                await Agent(
                    client.model, output_type=output_type(snapshot("openai", script.url)), retries=0
                ).run("Return JSON.", model_settings=client.settings)

        async def scenario() -> None:
            tasks = []
            try:
                deadline = time.monotonic() + 8
                # Ramp to forty held physical requests. A simultaneous SYN
                # burst measures the loopback server's listen backlog rather
                # than the shared capacity we are proving here.
                for index in range(40):
                    tasks.append(asyncio.create_task(occupy()))
                    while len(script.requests) < index + 1:
                        assert time.monotonic() < deadline, (
                            len(script.requests),
                            len(NETWORK_WORK.active),
                        )
                        await asyncio.sleep(0.005)
                assert len(NETWORK_WORK.active) == 40
                with pytest.raises(TimeoutError):
                    providers._bounded_http_read(Request(script.url, data=b"{}"), 0.02)
                async with model_client(
                    snapshot("openai", script.url), time.monotonic() + 0.05
                ) as extra:
                    with pytest.raises(AiProviderError) as caught:
                        await Agent(
                            extra.model,
                            output_type=output_type(snapshot("openai", script.url)),
                            retries=0,
                        ).run("Return JSON.", model_settings=extra.settings)
                    assert caught.value.code == "ai_timeout"
                assert len(script.requests) == 40
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        try:
            asyncio.run(scenario())
            assert len(NETWORK_WORK.active) == 40
            assert not permits.acquire(blocking=False)
            assert all(not client.transport.closed.is_set() for client in clients)
        finally:
            release.set()
        assert all(client.transport.closed.wait(2) for client in clients)
        deadline = time.monotonic() + 2
        while NETWORK_WORK.active and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not NETWORK_WORK.active
        for _ in range(40):
            assert permits.acquire(blocking=False)
        for _ in range(40):
            permits.release()


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_http_image_failure_precedes_reasoning_and_preserves_one_request(
    protocol: Protocol,
) -> None:
    with server(Reply(b"untrusted image rejection", 422)) as script:
        with pytest.raises(AiProviderError) as caught:
            asyncio.run(run(snapshot(protocol, script.url), media=True))
        assert caught.value.code == "ai_attachment_image_unsupported"
        assert caught.value.http_status == 422
        assert len(script.requests) == 1
