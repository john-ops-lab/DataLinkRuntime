"""DLR profiles mapped to official SDKs; no environment credential fallback."""

import socket
import ssl
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Annotated

import httpx2
from anthropic import AsyncAnthropic
from google.genai import Client
from google.genai.types import HttpOptions, HttpRetryOptions
from openai import AsyncOpenAI
from pydantic import WithJsonSchema
from pydantic_ai import NativeOutput, PromptedOutput
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from dlr.common.config import settings
from dlr.control.ai import providers
from dlr.control.ai.provider_config import AiProviderError, ProviderSnapshot, normalize_base_url
from dlr.control.ai.runtime.pydantic_ai.transport import (
    RAW_CAPTURE,
    OwnedTransport,
    ResponseCapture,
)
from dlr.control.schemas.ai import AiModelOutput


def _transport_schema() -> dict[str, object]:
    schema = providers.code_only_output_schema()
    definitions = schema.pop("$defs", {})
    assert isinstance(definitions, dict)

    def inline(value: object) -> object:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                return inline(definitions[reference.removeprefix("#/$defs/")])
            return {key: inline(item) for key, item in value.items()}
        if isinstance(value, list):
            return [inline(item) for item in value]
        return value

    result = inline(schema)
    assert isinstance(result, dict)
    return result


Output = Annotated[AiModelOutput, WithJsonSchema(_transport_schema())]


def output_type(
    snapshot: ProviderSnapshot,
) -> NativeOutput[AiModelOutput] | PromptedOutput[AiModelOutput]:
    if snapshot.adapter.structured_output == "json_schema":
        return NativeOutput(Output, name="dlr_assist", strict=True, template=False)
    return PromptedOutput(Output, template=False)


def safe_error(
    error: Exception, snapshot: ProviderSnapshot, *, image_input: bool = False
) -> AiProviderError:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, AiProviderError):
            return current
        if isinstance(current, ModelHTTPError):
            status = current.status_code
            code = "ai_provider_http_error"
            if status in (401, 403):
                code = "ai_auth_failed"
            elif status in (408, 504):
                code = "ai_timeout"
            elif status == 404:
                code = "ai_model_not_found"
            elif image_input and status in (400, 422):
                code = "ai_attachment_image_unsupported"
            elif snapshot.draft().reasoning_mode != "default" and status in (400, 422):
                code = "ai_reasoning_unsupported"
            return AiProviderError(code, transport_category="http", http_status=status)
        if isinstance(current, socket.gaierror):
            return AiProviderError("ai_provider_dns_failed", transport_category="dns")
        if isinstance(current, ssl.SSLError):
            return AiProviderError("ai_provider_unreachable", transport_category="tls")
        if isinstance(current, TimeoutError | httpx2.TimeoutException):
            return AiProviderError("ai_timeout", transport_category="timeout")
        current = current.__cause__ or current.__context__
    if isinstance(error, UnexpectedModelBehavior | ValueError):
        return AiProviderError("ai_response_invalid")
    return AiProviderError("ai_provider_unreachable", transport_category="connection")


@dataclass
class ModelClient:
    model: Model
    transport: OwnedTransport
    settings: ModelSettings


class SdkModel(WrapperModel):
    """Per-call profile policy around the SDK's public request interface."""

    def __init__(self, wrapped: Model, snapshot: ProviderSnapshot) -> None:
        super().__init__(wrapped)
        self.snapshot = snapshot

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        current = ModelSettings(**(model_settings or {}))
        if self.snapshot.adapter.split_reasoning:
            existing = current.get("extra_body")
            extra_body: dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
            extra_body["reasoning_split"] = not bool(model_request_parameters.function_tools)
            current["extra_body"] = extra_body
        try:
            capture = ResponseCapture()
            token = RAW_CAPTURE.set(capture)
            try:
                response = await self.wrapped.request(messages, current, model_request_parameters)
            finally:
                RAW_CAPTURE.reset(token)
            if capture.final_text is not None:
                # SDK thinking-tag splitting must never alter literal tags
                # inside JSON code strings. Raw DLR final validation owns this.
                response = replace(response, parts=[TextPart(capture.final_text)])
            elif (
                capture.tool_arguments is not None
                and self.snapshot.adapter.protocol != "openai_compatible"
            ):
                # Native SDKs may coerce/reject non-object arguments. Restore
                # the raw pre-SDK values for DLR dispatch and its D6 ledger.
                calls = [part for part in response.parts if isinstance(part, ToolCallPart)]
                if len(calls) != len(capture.tool_arguments):
                    raise AiProviderError("ai_response_invalid")
                raw_args = iter(capture.tool_arguments)
                response = replace(
                    response,
                    parts=[
                        replace(part, args=next(raw_args))
                        if isinstance(part, ToolCallPart)
                        else part
                        for part in response.parts
                    ],
                )
            return response
        except Exception as error:
            image_input = any(
                isinstance(item, BinaryContent) and item.media_type.startswith("image/")
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, UserPromptPart | ToolReturnPart)
                for item in (part.content if isinstance(part.content, list | tuple) else [])
            )
            raise safe_error(error, self.snapshot, image_input=image_input) from None


@asynccontextmanager
async def model_client(
    snapshot: ProviderSnapshot, hard_deadline: float
) -> AsyncIterator[ModelClient]:
    draft, adapter = snapshot.draft(), snapshot.adapter
    timeout = min(
        settings.ai_provider_timeout_seconds, max(0.001, hard_deadline - time.monotonic())
    )
    transport = OwnedTransport(
        protocol=adapter.protocol,
        authenticated=bool(snapshot.api_key),
        hard_deadline=hard_deadline,
        request_limit=adapter.max_request_bytes,
    )
    # Explicit dummy credentials satisfy SDK constructors for anonymous custom
    # services; the transport strips their auth headers before any network I/O.
    key = snapshot.api_key or "dlr-anonymous-no-environment-credential"
    base = normalize_base_url(draft.base_url)
    model_settings: ModelSettings = {"timeout": timeout}
    async with httpx2.AsyncClient(
        transport=transport, timeout=timeout, trust_env=False, follow_redirects=False
    ) as client:
        if adapter.protocol == "openai_compatible":
            extra: dict[str, object] = {}
            if adapter.split_reasoning:
                extra["reasoning_split"] = not adapter.tools_supported
            if draft.reasoning_mode != "default":
                if adapter.reasoning_style == "openai_effort":
                    extra["reasoning_effort"] = draft.reasoning_effort
                elif adapter.reasoning_style == "thinking":
                    extra["thinking"] = {"type": draft.reasoning_mode}
                    if draft.reasoning_effort is not None:
                        extra["reasoning_effort"] = draft.reasoning_effort
            model_settings["extra_body"] = extra
            sdk = AsyncOpenAI(api_key=key, base_url=base + "/v1", http_client=client, max_retries=0)
            model: Model = OpenAIChatModel(
                draft.model,
                provider=OpenAIProvider(openai_client=sdk),
                profile=OpenAIModelProfile(
                    supports_json_schema_output=True,
                    supports_json_object_output=adapter.structured_output == "json_object",
                    supports_forced_tool_choice=True,
                    openai_unsupported_model_settings=[],
                ),
            )
            yield ModelClient(SdkModel(model, snapshot), transport, model_settings)
        elif adapter.protocol == "anthropic":
            sdk_a = AsyncAnthropic(api_key=key, base_url=base, http_client=client, max_retries=0)
            model_settings["max_tokens"] = 4096
            model = AnthropicModel(
                draft.model,
                provider=AnthropicProvider(anthropic_client=sdk_a),
                profile={"supports_json_schema_output": True},
            )
            yield ModelClient(SdkModel(model, snapshot), transport, model_settings)
        else:
            # Keep an existing /v1beta suffix single, including gateway prefixes.
            if base.endswith("/v1beta"):
                base = base[:-7]
            google = Client(
                vertexai=False,
                api_key=key,
                http_options=HttpOptions(
                    base_url=base,
                    api_version="v1beta",
                    httpx_async_client=client,
                    timeout=int(timeout * 1000),
                    retry_options=HttpRetryOptions(attempts=1),
                ),
            )
            try:
                model = GoogleModel(
                    draft.model,
                    provider=GoogleProvider(client=google),
                    profile={
                        "supports_json_schema_output": True,
                        "supports_json_object_output": True,
                    },
                )
                yield ModelClient(SdkModel(model, snapshot), transport, model_settings)
            finally:
                await google.aio.aclose()
