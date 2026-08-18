"""Strict request and response schemas for the M4 AI Editor boundary."""

import math
from datetime import datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from dlr.control.ai.attachments import MAX_ATTACHMENTS, MAX_FILE_BYTES

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


class AiAttachment(_StrictSchema):
    """M5.7 Wave B2: one browser-uploaded attachment for this request only.

    The file body travels as strict base64 inside the existing JSON assist
    request. Attachments are validated, bounded and (for PDF / DOCX / text /
    code) parsed server-side; they exist only for the current request and are
    never persisted, never written to temp files and never logged. The
    filename is display metadata only and is sanitized before it can join the
    Provider context. Structural checks below keep the stable error contract;
    byte size, magic-byte, archive-safety and parse checks happen in the
    service layer.
    """

    filename: str
    content_type: str
    data_base64: str

    @model_validator(mode="before")
    @classmethod
    def reject_malformed_attachment(cls, value: object) -> object:
        # A malformed entry (non-object, unknown keys, or any missing
        # required key) is rejected here with the stable sanitized code.
        # Without this, pydantic's plain ValidationError would let FastAPI's
        # default 422 renderer return a list detail instead of the stable
        # {code, message} shape and echo the offending input — including the
        # whole attachment dict with its base64 body — back to the browser.
        if not isinstance(value, dict):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_attachment_invalid",
                    "message": "AI request contains an invalid attachment",
                },
            ) from None
        required = {"filename", "content_type", "data_base64"}
        if not required.issubset(value) or any(key not in required for key in value):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_attachment_invalid",
                    "message": "AI request contains an invalid attachment",
                },
            ) from None
        return value

    @field_validator("filename", mode="before")
    @classmethod
    def validate_filename(cls, value: object) -> str:
        # Blank/oversized filenames are rejected here with the stable code and
        # never echoed; path/traversal/control-char checks live in the service
        # layer where the sanitized display name is produced.
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_attachment_invalid",
                    "message": "AI request contains an invalid attachment",
                },
            ) from None
        if len(value) > 255:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_attachment_invalid",
                    "message": "AI request contains an invalid attachment",
                },
            ) from None
        return value

    @field_validator("content_type", mode="before")
    @classmethod
    def validate_content_type(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_attachment_invalid",
                    "message": "AI request contains an invalid attachment",
                },
            ) from None
        return value

    @field_validator("data_base64", mode="before")
    @classmethod
    def validate_data_base64(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_attachment_invalid",
                    "message": "AI request contains an invalid attachment",
                },
            ) from None
        # Cheap pre-guard against oversized JSON bodies; the exact decoded
        # byte limit is enforced in the service layer.
        if len(value) > (MAX_FILE_BYTES * 4 // 3) + 8:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_attachment_too_large",
                    "message": "AI request attachment exceeds the size limit",
                },
            ) from None
        return value


class AiContextSnippet(_StrictSchema):
    """M5.5.13: one exact browser-captured context snippet added to the AI
    context (Monaco code selection or a selection of the browser-visible,
    already-masked live-log text).

    The browser captures the text and the 1-based line range at the moment
    the administrator clicks "加入对话上下文"; later cursor movement must
    never change this snapshot. ``source`` distinguishes a Working Copy code
    selection from a masked live-log selection. Snippets are explicit,
    administrator-provided excerpts only, never a path to other files.
    """

    source: Literal["code", "log"]
    text: str
    start_line: int
    end_line: int

    @field_validator("source", mode="before")
    @classmethod
    def validate_source(cls, value: object) -> str:
        # Validated here (instead of relying on Literal's default error) so
        # an invalid source yields the stable code and never echoes the raw
        # offending value in a FastAPI validation detail.
        if value not in ("code", "log"):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_request_invalid",
                    "message": "AI request contains an invalid context snippet",
                },
            ) from None
        return value

    @field_validator("text", mode="before")
    @classmethod
    def validate_text(cls, value: object) -> str:
        # Strip is used only to decide "blank"; the returned value keeps the
        # exact administrator-selected text, including leading indentation
        # and trailing newlines, which are meaningful in code. Log snippets
        # are already-masked browser-visible text; raw logs never join.
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_request_invalid",
                    "message": "AI request contains an invalid context snippet",
                },
            ) from None
        if len(value) > 50_000:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_request_invalid",
                    "message": "AI request contains an invalid context snippet",
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
                    "message": "AI request contains an invalid context snippet",
                },
            ) from None
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "AiContextSnippet":
        if self.end_line < self.start_line:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_request_invalid",
                    "message": "AI request contains an invalid context snippet",
                },
            )
        return self


class AiAssistRequest(_StrictSchema):
    message: str
    working_copy: AiWorkingCopy
    recent_messages: list[AiRecentMessage] = Field(default_factory=list, max_length=8)
    base_version_id: int | None = None
    # M5.5.13: ordered multi-snippet context (code + code, code + log, ...).
    # The browser sends the snippets in the order the administrator added
    # them; they are never persisted and never leak across Adapter switches.
    context_snippets: list[AiContextSnippet] = Field(default_factory=list)
    # M5.7 Wave B2: request-only attachments (base64 bodies). Omitted or
    # empty keeps the pre-attachment request contract byte-for-byte.
    attachments: list[AiAttachment] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_snippet_count(self) -> "AiAssistRequest":
        # Validated here (instead of a Field max_length) so an over-limit
        # list yields the stable code and never echoes the raw payload.
        if len(self.context_snippets) > 20:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_request_invalid",
                    "message": "AI request contains an invalid context snippet",
                },
            )
        return self

    @model_validator(mode="after")
    def validate_attachment_count(self) -> "AiAssistRequest":
        if len(self.attachments) > MAX_ATTACHMENTS:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ai_attachment_count_exceeded",
                    "message": "AI request contains too many attachments",
                },
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def reject_unicode_surrogates(cls, value: object) -> object:
        # Runs before nested schemas so Working Copy strings, runtime_config
        # keys/values, recent conversation content and the context snippets
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


class AiAttachmentLimits(_StrictSchema):
    """M5.7 Wave B2: bounded attachment limits for the Wave B3 upload UI."""

    max_attachments: int
    max_file_bytes: int
    max_total_bytes: int
    max_parsed_chars_per_file: int
    max_parsed_total_chars: int
    parse_timeout_seconds: float


class AiProviderAttachmentCapability(_StrictSchema):
    """Per-Provider native attachment capability (explicit, never assumed).

    ``images_native`` means the Provider adapter sends images through its
    native multimodal content parts; ``files_native`` means the Provider
    accepts raw files. Only capability-table truth enables native input;
    everything else goes to the bounded server-side fallback or a stable,
    actionable error.
    """

    provider: AiProvider
    images_native: bool
    files_native: bool


class AiAttachmentCapabilitiesResponse(_StrictSchema):
    """Stable Wave B3 contract: limits, accepted MIME types and Provider
    capability table for the current build."""

    limits: AiAttachmentLimits
    supported_content_types: list[str]
    providers: list[AiProviderAttachmentCapability]
