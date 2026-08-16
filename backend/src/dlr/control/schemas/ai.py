"""Strict request and response schemas for the M4 AI Editor boundary."""

import math
from datetime import datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

AiProvider = Literal[
    "openai",
    "deepseek",
    "kimi",
    "minimax",
    "custom_openai_compatible",
]
ReasoningMode = Literal["default", "enabled", "disabled"]
ReasoningEffort = Literal["low", "medium", "high", "max", "xhigh"]


class _StrictSchema(BaseModel):
    """AI boundary objects reject unknown fields instead of guessing intent."""

    model_config = ConfigDict(extra="forbid")


def contains_unicode_surrogate(value: object) -> bool:
    """Return whether any nested string or object key contains U+D800-U+DFFF."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in item):
                return True
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return False


def _reject_request_surrogates(value: object) -> object:
    """Reject invalid Unicode without reflecting the unsafe input in a 422."""
    if contains_unicode_surrogate(value):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "ai_request_invalid",
                "message": "AI request contains invalid Unicode",
            },
        )
    return value


def _non_blank(value: object, field_name: str, max_length: int | None = None) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be blank")
    if max_length is not None and len(stripped) > max_length:
        raise ValueError(f"{field_name} must be at most {max_length} characters")
    return stripped


def _base_url(value: object) -> str:
    base_url = _non_blank(value, "base_url")
    return base_url.rstrip("/")


def _finite_json_object(value: object) -> object:
    """Reject non-JSON and non-finite values at every nesting level."""
    if not isinstance(value, dict):
        raise ValueError("runtime_config must be a JSON object")
    pending: list[object] = [value]
    while pending:
        item = pending.pop()
        if item is None or isinstance(item, (bool, str, int)):
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("runtime_config numbers must be finite")
            continue
        if isinstance(item, list):
            pending.extend(item)
            continue
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("runtime_config keys must be strings")
            pending.extend(item.values())
            continue
        raise ValueError("runtime_config must contain JSON values only")
    return value


class AiProviderDraft(_StrictSchema):
    """Provider fields shared by model refresh and the persisted setting."""

    provider: AiProvider
    base_url: str
    credential_id: int | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_unicode_surrogates(cls, value: object) -> object:
        return _reject_request_surrogates(value)

    @field_validator("base_url", mode="before")
    @classmethod
    def validate_base_url(cls, value: object) -> str:
        return _base_url(value)


class AiSettingDraft(AiProviderDraft):
    """Complete replace/upsert body for the singleton AI setting."""

    model: str
    reasoning_mode: ReasoningMode = "default"
    reasoning_effort: ReasoningEffort | None = None

    @field_validator("model", mode="before")
    @classmethod
    def normalize_model(cls, value: object) -> str:
        return _non_blank(value, "model", 256)


class AiSettingResponse(AiSettingDraft):
    """Persisted metadata; no API key or credential ciphertext is exposed."""

    id: int
    credential_name: str | None = None
    created_at: datetime
    updated_at: datetime


class AiModelsResponse(_StrictSchema):
    models: list[str]


class AiConnectionTestResponse(_StrictSchema):
    ok: bool
    message: str


class AiWorkingCopy(_StrictSchema):
    """The browser-owned authoritative snapshot for one assist request."""

    code: str
    requirements: str = ""
    runtime_config: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("runtime_config", mode="before")
    @classmethod
    def validate_runtime_config(cls, value: object) -> object:
        try:
            return _finite_json_object(value)
        except ValueError:
            # FastAPI's default validation error renderer cannot serialize a
            # NaN/Infinity input value. Return a stable, input-free 422 at the
            # schema boundary instead of letting error rendering become a 500.
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_working_copy_invalid",
                    "message": "Working Copy runtime_config must contain finite JSON values",
                },
            ) from None


class AiRecentMessage(_StrictSchema):
    role: Literal["user", "assistant"]
    content: str


class AiSelectionContext(_StrictSchema):
    """M5.5.5: exact Monaco selection snapshot added to the AI context.

    The browser captures the text and the 1-based Monaco line range at the
    moment the administrator clicks "加入对话上下文"; later cursor movement
    must never change this snapshot. It is an explicit, administrator-provided
    excerpt of the current Working Copy only, never a path to other files.
    """

    text: str
    start_line: int
    end_line: int

    @field_validator("text", mode="before")
    @classmethod
    def validate_text(cls, value: object) -> str:
        # Strip is used only to decide "blank"; the returned value keeps the
        # exact administrator-selected text, including leading indentation
        # and trailing newlines, which are meaningful in code.
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_request_invalid",
                    "message": "AI request contains an invalid selection context",
                },
            ) from None
        return value

    @field_validator("start_line", "end_line", mode="before")
    @classmethod
    def validate_line(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_request_invalid",
                    "message": "AI request contains an invalid selection context",
                },
            ) from None
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "AiSelectionContext":
        if self.end_line < self.start_line:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_request_invalid",
                    "message": "AI request contains an invalid selection context",
                },
            )
        return self


class AiAssistRequest(_StrictSchema):
    message: str
    working_copy: AiWorkingCopy
    recent_messages: list[AiRecentMessage] = Field(default_factory=list, max_length=8)
    base_version_id: int | None = None
    selected_context: AiSelectionContext | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_unicode_surrogates(cls, value: object) -> object:
        # Runs before nested schemas so Working Copy strings, runtime_config
        # keys/values, recent conversation content and the selected context
        # share one safe boundary.
        return _reject_request_surrogates(value)

    @field_validator("message", mode="before")
    @classmethod
    def validate_message(cls, value: object) -> str:
        return _non_blank(value, "message")


class AiCandidate(_StrictSchema):
    """A complete candidate snapshot. It never carries lifecycle fields."""

    summary: str
    code: str
    requirements: str
    runtime_config: dict[str, JsonValue]
    required_secret_keys: list[str]

    @field_validator("runtime_config", mode="before")
    @classmethod
    def validate_runtime_config(cls, value: object) -> object:
        return _finite_json_object(value)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("code must not be blank")
        return value


class AiModelOutput(_StrictSchema):
    """The only JSON shape accepted from a provider final answer."""

    message: str
    candidate: AiCandidate | None

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class AiAssistResponse(AiModelOutput):
    """Browser response enriched only with non-sensitive routing metadata."""

    provider: AiProvider
    model: str
