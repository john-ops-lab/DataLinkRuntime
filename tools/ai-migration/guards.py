"""Small public extension-point experiments, deliberately outside DLR runtime."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx2
from dlr.control.ai import providers
from dlr.control.ai.diagnostics import StrictJsonError
from dlr.control.schemas.ai import contains_unicode_surrogate
from pydantic import ValidationError
from pydantic_ai.capabilities import AbstractCapability, OutputContext
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import RunContext

from probe_support import CANARY, Final


class Rejected(Exception):
    """Only a stable stage/reason is carried across the experimental boundary."""

    def __init__(self, stage: str, reason: str):
        self.stage = stage
        self.reason = reason
        super().__init__(stage + ":" + reason)


def strict_json(raw: str | bytes, stage: str) -> Any:
    try:
        value = providers.load_json_strict(raw)
    except StrictJsonError as error:
        raise Rejected(stage, error.reason) from None
    except (ValueError, UnicodeError):
        raise Rejected(stage, "json_invalid") from None
    if contains_unicode_surrogate(value):
        raise Rejected(stage, "unicode_invalid")
    return value


class GuardedTransport(httpx2.AsyncBaseTransport):
    """Inspect bounded bytes before any SDK can discard duplicate keys."""

    def __init__(
        self,
        *,
        request_limit: int = 32 << 20,
        response_limit: int = 4 << 20,
        permits: threading.BoundedSemaphore | None = None,
        capacity_timeout: float = 2,
    ):
        self.inner = httpx2.AsyncHTTPTransport(retries=0, trust_env=False)
        self.request_limit = request_limit
        self.response_limit = response_limit
        self.permits = permits if permits is not None else providers._TRANSPORT_PERMITS
        self.capacity_timeout = capacity_timeout

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if len(await request.aread()) > self.request_limit:
            raise Rejected("http_request", "body_limit")
        deadline = time.monotonic() + self.capacity_timeout
        while not self.permits.acquire(blocking=False):
            if time.monotonic() >= deadline:
                raise Rejected("capacity", "timeout")
            await asyncio.sleep(min(0.005, max(0, deadline - time.monotonic())))
        try:
            return await self.read_response(request)
        finally:
            # Intentionally tested as insufficient for detached DNS work.
            # Do not promote this thin wrapper into the production runtime.
            self.permits.release()

    async def read_response(self, request: httpx2.Request) -> httpx2.Response:
        response = await self.inner.handle_async_request(request)
        raw = bytearray()
        try:
            async for chunk in response.aiter_raw():
                raw.extend(chunk)
                if len(raw) > self.response_limit:
                    raise Rejected("http_envelope", "body_limit")
            strict_json(bytes(raw), "http_envelope")
            return httpx2.Response(
                response.status_code, headers=response.headers, content=bytes(raw), request=request
            )
        finally:
            await response.aclose()

    async def aclose(self) -> None:
        await self.inner.aclose()


@dataclass
class Observation:
    events: list[str] = field(default_factory=list)
    batches: list[int] = field(default_factory=list)
    messages_seen: list[int] = field(default_factory=list)
    validated_tools: list[str] = field(default_factory=list)


class ContentGuard(AbstractCapability[Observation]):
    async def before_model_request(
        self, ctx: RunContext[Observation], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        ctx.deps.events.append("before_model")
        ctx.deps.messages_seen.append(len(request_context.messages))
        return request_context

    async def after_model_request(
        self,
        ctx: RunContext[Observation],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        ctx.deps.events.append("after_model")
        calls = [part for part in response.parts if isinstance(part, ToolCallPart)]
        if calls:
            ctx.deps.batches.append(len(calls))
            for call in calls:
                if isinstance(call.args, str):
                    strict_json(call.args, "tool_args")
        return response

    async def before_output_validate(
        self, ctx: RunContext[Observation], *, output_context: OutputContext, output: Any
    ) -> Any:
        ctx.deps.events.append("before_output_parse")
        value = strict_json(output, "final_json") if isinstance(output, str | bytes) else output
        if CANARY in json.dumps(value, ensure_ascii=True):
            raise Rejected("final_json", "secret_reflection")
        try:
            Final.model_validate(value, strict=True)
        except ValidationError:
            raise Rejected("output_schema", "invalid") from None
        return output


def cause_is(error: BaseException, stage: str, reason: str | None = None) -> bool:
    """SDKs may wrap transport faults; assertions inspect the cause, not str()."""
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, Rejected) and current.stage == stage:
            return reason is None or current.reason == reason
        current = current.__cause__
    return False
