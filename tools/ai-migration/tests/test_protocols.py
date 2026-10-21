"""P0/P1/P2/P3/P7: real model/SDK wire paths and pre-parse failures."""

import asyncio
import base64
import hashlib
import json
from typing import Any

import pytest
from pydantic_ai import Agent, BinaryContent, NativeOutput, PromptedOutput, ToolReturn
from pydantic_ai.exceptions import ModelHTTPError

from guards import ContentGuard, GuardedTransport, Observation, cause_is
from probe_support import (
    CANARY,
    PNG,
    PROTOCOLS,
    Final,
    Mode,
    Protocol,
    Reply,
    model,
    pdf,
    response,
    server,
)


def media_blobs(value: Any) -> list[bytes]:
    """Decode actual wire media, including Gemini's URL-safe base64."""
    blobs: list[bytes] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"data", "file_data", "url"} and isinstance(item, str):
                encoded = item.split(";base64,", 1)[-1]
                try:
                    decoded = base64.urlsafe_b64decode(encoded)
                except ValueError:
                    continue
                if decoded.startswith((b"\x89PNG", b"%PDF")):
                    blobs.append(decoded)
            blobs.extend(media_blobs(item))
    elif isinstance(value, list):
        for item in value:
            blobs.extend(media_blobs(item))
    return blobs


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("mode", ["prompt_only", "json_schema"])
def test_sdk_roundtrip_uses_configured_url_auth_and_output_mode(
    protocol: Protocol, mode: Mode
) -> None:
    with server(response(protocol)) as script:
        observation = Observation()

        async def run() -> None:
            async with model(protocol, script.url, mode=mode, transport=GuardedTransport()) as sdk:
                output = NativeOutput(Final) if mode == "json_schema" else PromptedOutput(Final)
                result = await Agent(
                    sdk, output_type=output, retries=0, capabilities=[ContentGuard()]
                ).run("Synthetic consultation", deps=observation)
                assert result.output.message == "B0 answer"

        asyncio.run(run())
        assert len(script.requests) == 1
        request = script.requests[0]
        body = request.json()
        assert "/gateway/" in request.path
        if protocol == "openai":
            assert request.path == "/gateway/v1/chat/completions"
            assert request.headers["authorization"] == "Bearer " + CANARY
            assert body.get("response_format", {}).get("type") == (
                "json_schema" if mode == "json_schema" else None
            )
        elif protocol == "anthropic":
            # The adapter uses the SDK beta endpoint: this is a measured
            # difference from legacy, not proof that custom gateways accept it.
            assert request.path == "/gateway/v1/messages?beta=true"
            assert request.headers["x-api-key"] == CANARY
            assert ("output_config" in body) == (mode == "json_schema")
        else:
            assert request.path == "/gateway/v1beta/models/gemini-2.5-flash:generateContent"
            assert request.headers["x-goog-api-key"] == CANARY
            config = body.get("generationConfig", {})
            assert bool(config.get("responseJsonSchema")) == (mode == "json_schema")
        # No result-submission tool or legacy mutable configuration is exposed.
        assert not body.get("tools")
        assert b"required_secret_keys" in request.body
        assert b"runtime_config" not in request.body
        assert observation.events == ["before_model", "after_model", "before_output_parse"]


def test_openai_json_object_is_not_silently_replaced_by_output_tool() -> None:
    with server(response("openai")) as script:

        async def run() -> None:
            async with model("openai", script.url, mode="json_object") as sdk:
                result = await Agent(sdk, output_type=PromptedOutput(Final), retries=0).run(
                    "Synthetic consultation"
                )
                assert result.output.candidate is None

        asyncio.run(run())
        assert script.requests[0].json()["response_format"] == {"type": "json_object"}
        assert not script.requests[0].json().get("tools")


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize(
    ("fragment", "reason"),
    [
        (b'"b0":1,"b0":2,', "duplicate_key"),
        (b'"b0":NaN,', "non_finite_number"),
        (b'"b0":"\\ud800",', "unicode_invalid"),
    ],
)
def test_outer_bytes_are_rejected_before_sdk_parsing(
    protocol: Protocol, fragment: bytes, reason: str
) -> None:
    reply = response(protocol)
    reply.body = b"{" + fragment + reply.body[1:]
    with server(reply) as script:

        async def run() -> None:
            async with model(protocol, script.url, transport=GuardedTransport()) as sdk:
                with pytest.raises(Exception) as caught:
                    await Agent(sdk, output_type=PromptedOutput(Final), retries=0).run(
                        "Synthetic input"
                    )
                assert cause_is(caught.value, "http_envelope", reason)

        asyncio.run(run())
        assert len(script.requests) == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize(
    "text",
    [
        '{"message":"first","message":"last","candidate":null}',
        '{"message":3,"candidate":null}',
        '{"message":"\\ud800","candidate":null}',
        '{"message":"' + CANARY + '","candidate":null}',
        '{"message":"ok","candidate":null,"unexpected":true}',
    ],
)
def test_invalid_final_aborts_without_automatic_retry_or_raw_reflection(
    protocol: Protocol, text: str
) -> None:
    with server(response(protocol, text)) as script:

        async def run() -> None:
            async with model(protocol, script.url, transport=GuardedTransport()) as sdk:
                with pytest.raises(Exception) as caught:
                    await Agent(
                        sdk,
                        output_type=PromptedOutput(Final),
                        retries=0,
                        capabilities=[ContentGuard()],
                    ).run("Synthetic input", deps=Observation())
                assert cause_is(caught.value, "final_json") or cause_is(
                    caught.value, "output_schema"
                )

        asyncio.run(run())
        assert len(script.requests) == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("status", [429, 503])
def test_explicit_sdk_retry_off_means_one_http_attempt(protocol: Protocol, status: int) -> None:
    with server(Reply(b'{"error":{"message":"synthetic outage"}}', status)) as script:

        async def run() -> None:
            async with model(protocol, script.url, transport=GuardedTransport()) as sdk:
                with pytest.raises(ModelHTTPError) as caught:
                    await Agent(sdk, retries=0).run("Synthetic input")
                assert caught.value.status_code == status

        asyncio.run(run())
        assert len(script.requests) == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_initial_media_and_tool_return_media_reach_next_sdk_request(protocol: Protocol) -> None:
    document = pdf()
    tool_reply = response(protocol, calls=[("read_page", "b0-call", "{}")])
    with server(tool_reply, response(protocol)) as script:

        async def read_page() -> ToolReturn:
            return ToolReturn(
                return_value={"source": "synthetic/page/1"},
                content=[BinaryContent(PNG, media_type="image/png")],
            )

        async def run() -> None:
            async with model(protocol, script.url, transport=GuardedTransport()) as sdk:
                result = await Agent(
                    sdk,
                    tools=[read_page],
                    output_type=PromptedOutput(Final),
                    retries=0,
                ).run(
                    [
                        "Read synthetic page 1",
                        BinaryContent(PNG, media_type="image/png"),
                        BinaryContent(document, media_type="application/pdf"),
                    ]
                )
                assert result.output.message == "B0 answer"

        asyncio.run(run())
        assert len(script.requests) == 2
        initial, continuation = (request.body for request in script.requests)
        for body in (initial, continuation):
            blobs = media_blobs(json.loads(body))
            assert hashlib.sha256(PNG).hexdigest() in [
                hashlib.sha256(blob).hexdigest() for blob in blobs
            ]
            assert document in blobs
        assert b"b0-call" in continuation
        assert b"synthetic/page/1" in continuation
        assert media_blobs(json.loads(continuation)).count(PNG) == 2
        assert media_blobs(json.loads(continuation)).count(document) == 1
