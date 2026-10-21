"""Public HTTP transport guard, before SDK parsing or coercion."""

import asyncio
import json
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx2

from dlr.control.ai import providers
from dlr.control.ai.diagnostics import StrictJsonError
from dlr.control.ai.provider_config import (
    MAX_PROVIDER_REQUEST_BYTES,
    MAX_PROVIDER_RESPONSE_BYTES,
    AiProviderError,
    Protocol,
)
from dlr.control.ai.runtime.pydantic_ai.work import NETWORK_WORK, WorkOwner
from dlr.control.schemas.ai import AiProvider


@dataclass
class ResponseCapture:
    final_text: str | None = field(default=None, repr=False)
    tool_arguments: tuple[str, ...] | None = field(default=None, repr=False)


RAW_CAPTURE: ContextVar[ResponseCapture | None] = ContextVar("dlr_sdk_raw_capture", default=None)


def inspect_envelope(raw: bytes, protocol: Protocol) -> object:
    try:
        value = providers.load_json_strict(raw)
    except StrictJsonError as error:
        raise AiProviderError(
            "ai_response_invalid", stage="provider_json", reason=error.reason
        ) from None
    except UnicodeDecodeError:
        raise AiProviderError(
            "ai_response_invalid", stage="provider_json", reason="invalid_utf8"
        ) from None
    except json.JSONDecodeError:
        raise AiProviderError(
            "ai_response_invalid", stage="provider_json", reason="malformed_json"
        ) from None
    except RecursionError:
        raise AiProviderError(
            "ai_response_invalid", stage="provider_json", reason="json_limit"
        ) from None
    except ValueError:
        raise AiProviderError(
            "ai_response_invalid", stage="provider_json", reason="json_value_invalid"
        ) from None
    # During coexistence this is the shared, strict envelope contract. It
    # validates raw field types; the SDK alone creates conversation messages.
    provider: AiProvider = (
        "anthropic" if protocol == "anthropic" else "gemini" if protocol == "gemini" else "openai"
    )
    final, calls = providers.extract_round(provider, value)
    capture = RAW_CAPTURE.get()
    if capture is not None and calls is None:
        capture.final_text = final
    elif capture is not None and calls is not None:
        capture.tool_arguments = tuple(call.arguments for call in calls)
    return value


def sdk_metadata(value: object, protocol: Protocol, request_body: bytes) -> bytes:
    """Adapt SDK parsing after strict raw capture, without changing DLR dispatch.

    Raw domain fields have already passed the original envelope contract.
    Missing non-domain metadata is supplied for minimal compatible gateways.
    Native non-object arguments use an SDK-only placeholder; SdkModel restores
    every original argument before DLR validation and budget accounting.
    """
    assert isinstance(value, dict)
    if protocol == "openai_compatible":
        value.setdefault("id", "dlr-compatible-response")
        value.setdefault("object", "chat.completion")
        value.setdefault("created", 0)
        value.setdefault("model", json.loads(request_body).get("model", "dlr-compatible-model"))
        for index, choice in enumerate(value["choices"]):
            choice.setdefault("index", index)
            choice["message"].setdefault("role", "assistant")
    elif protocol == "anthropic":
        value.setdefault("id", "dlr-compatible-response")
        value.setdefault("type", "message")
        value.setdefault("role", "assistant")
        value.setdefault("model", json.loads(request_body).get("model", "dlr-compatible-model"))
        value.setdefault("usage", {"input_tokens": 0, "output_tokens": 0})
        for block in value["content"]:
            if block.get("type") == "tool_use" and not isinstance(block.get("input", {}), dict):
                block["input"] = {}
    elif protocol == "gemini":
        for part in value["candidates"][0]["content"]["parts"]:
            call = part.get("functionCall")
            if isinstance(call, dict) and not isinstance(call.get("args", {}), dict):
                call["args"] = {}
    return json.dumps(value, ensure_ascii=True).encode()


class OwnedTransport(httpx2.AsyncBaseTransport):
    def __init__(
        self,
        *,
        protocol: Protocol,
        authenticated: bool,
        hard_deadline: float,
        request_limit: int = MAX_PROVIDER_REQUEST_BYTES,
        response_limit: int = MAX_PROVIDER_RESPONSE_BYTES,
        permits: threading.BoundedSemaphore | None = None,
        owner: WorkOwner = NETWORK_WORK,
        inspect_request: Callable[[bytes], None] | None = None,
    ) -> None:
        self.inner = httpx2.HTTPTransport(retries=0, trust_env=False)
        self.protocol = protocol
        self.authenticated = authenticated
        self.hard_deadline = hard_deadline
        self.request_limit = request_limit
        self.response_limit = response_limit
        self.permits = permits if permits is not None else providers._TRANSPORT_PERMITS
        self.owner = owner
        self.inspect_request = inspect_request
        self.lock = threading.Lock()
        self.active = 0
        self.closing = False
        self.closed = threading.Event()

    def finish(self) -> None:
        with self.lock:
            self.active -= 1
            if self.closing and self.active == 0:
                self.inner.close()
                self.closed.set()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        deadline = self.hard_deadline
        capture = RAW_CAPTURE.get()
        body = await request.aread()
        if len(body) > self.request_limit:
            raise AiProviderError("ai_context_over_budget")
        if self.inspect_request is not None:
            self.inspect_request(body)
        while not self.permits.acquire(blocking=False):
            if time.monotonic() >= deadline:
                raise AiProviderError("ai_timeout")
            await asyncio.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            self.permits.release()
            raise AiProviderError("ai_timeout")
        with self.lock:
            if self.closing:
                self.permits.release()
                raise AiProviderError("ai_provider_unreachable")
            self.active += 1
        headers = dict(request.headers)
        headers["accept-encoding"] = "identity"
        if not self.authenticated:
            for name in ("authorization", "x-api-key", "x-goog-api-key"):
                headers.pop(name, None)
        url = request.url
        if self.protocol == "anthropic":
            # The SDK's beta namespace adds this routing flag even for stable
            # Messages. Preserve DLR's stable endpoint on custom gateways.
            url = url.copy_remove_param("beta")
        physical = httpx2.Request(
            request.method, url, headers=headers, content=body, extensions=request.extensions
        )

        def read() -> httpx2.Response:
            try:
                response = self.inner.handle_request(physical)
                try:
                    if 300 <= response.status_code < 400:
                        raise AiProviderError(
                            "ai_provider_http_error",
                            transport_category="http",
                            http_status=response.status_code,
                        )
                    raw = bytearray()
                    for chunk in response.iter_raw():
                        if time.monotonic() >= deadline:
                            raise AiProviderError("ai_timeout")
                        if len(raw) + len(chunk) > self.response_limit:
                            raise AiProviderError(
                                "ai_response_invalid",
                                stage="provider_json",
                                reason="response_too_large",
                            )
                        raw.extend(chunk)
                    data = bytes(raw)
                    if 200 <= response.status_code < 300:
                        token = RAW_CAPTURE.set(capture)
                        try:
                            inspected = inspect_envelope(data, self.protocol)
                        finally:
                            RAW_CAPTURE.reset(token)
                        data = sdk_metadata(inspected, self.protocol, body)
                    if time.monotonic() >= deadline:
                        raise AiProviderError("ai_timeout")
                    return httpx2.Response(
                        response.status_code,
                        headers=response.headers,
                        content=data,
                        request=request,
                    )
                finally:
                    response.close()
            finally:
                try:
                    self.finish()
                finally:
                    self.permits.release()

        try:
            future = self.owner.submit(read)
        except BaseException:
            self.finish()
            self.permits.release()
            raise
        try:
            async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
                return await self.owner.result(future)
        except TimeoutError:
            raise AiProviderError("ai_timeout") from None

    async def aclose(self) -> None:
        with self.lock:
            self.closing = True
            if self.active == 0 and not self.closed.is_set():
                self.inner.close()
                self.closed.set()
