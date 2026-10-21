"""DLR-owned immutable request data and restricted runtime resources."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.orm import Session

from dlr.control.ai.attachment_reading import ScopedAttachmentReader
from dlr.control.schemas.ai import (
    AiAssistRequest,
    AiAssistResponse,
    AiModelOutput,
    AiProvider,
    AiToolCallSummary,
)


@dataclass(frozen=True)
class PreparedAssist:
    adapter_id: int
    request_json: str = field(repr=False)

    @classmethod
    def create(cls, adapter_id: int, request: AiAssistRequest) -> "PreparedAssist":
        return cls(adapter_id, request.model_dump_json(exclude_unset=True))

    def request(self) -> AiAssistRequest:
        return AiAssistRequest.model_validate_json(self.request_json)


@dataclass(frozen=True)
class RuntimeResources:
    # The legacy callback stays on the request thread. Session/Reader factories
    # are the only database/file access supplied to execution-thread tools.
    legacy_execute: Callable[[PreparedAssist], AiAssistResponse] = field(repr=False)
    session_factory: Callable[[], Session] | None = field(default=None, repr=False)
    reader_factory: Callable[[], AbstractContextManager[ScopedAttachmentReader]] | None = field(
        default=None, repr=False
    )
    permission_check: Callable[[], object] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class RuntimeOutcome:
    output: AiModelOutput
    provider: AiProvider
    model: str
    tool_calls: tuple[AiToolCallSummary, ...]

    @classmethod
    def from_response(cls, response: AiAssistResponse) -> "RuntimeOutcome":
        return cls(
            AiModelOutput(
                message=response.message,
                candidate=response.candidate,
                request_intent=response.request_intent,
            ),
            response.provider,
            response.model,
            tuple(response.tool_calls),
        )

    def response(self) -> AiAssistResponse:
        return AiAssistResponse(
            message=self.output.message,
            candidate=self.output.candidate,
            request_intent=self.output.request_intent,
            provider=self.provider,
            model=self.model,
            tool_calls=list(self.tool_calls),
        )


class AssistRuntime(Protocol):
    def execute(self, prepared: PreparedAssist, resources: RuntimeResources) -> RuntimeOutcome: ...
