"""Deeply isolated request facts for AI prompt assembly."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from dlr.control.ai import attachments as attachments_service
from dlr.control.schemas.ai import AiAssistRequest


def literal_text_fields(text: str, *, truncated: bool = False) -> list[dict[str, object]]:
    """Bounded lexical hints, never a semantic parser or instruction authority."""
    if truncated:
        return []
    fields: list[dict[str, object]] = []
    for number, line in enumerate(text.splitlines(), 1):
        separators = [index for mark in (":", "：") if (index := line.find(mark)) >= 0]
        if not separators:
            continue
        separator = min(separators)
        label, value = line[:separator].strip(" \t"), line[separator + 1 :].strip(" \t")
        if (
            not 1 <= len(label) <= 80
            or not 1 <= len(value) <= 256
            or not all(char.isalnum() or char in " _-./" for char in label)
        ):
            continue
        start = line.index(value, separator + 1)
        fields.append(
            {
                "label": label,
                "value": value,
                "line": number,
                "value_start": start,
                "value_end": start + len(value),
            }
        )
        if len(fields) == 8:
            break
    return fields


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class PromptContext:
    adapter_id: int
    language: str
    system_locale: str
    base_version: Mapping[str, object] | None
    available_secret_keys: tuple[str, ...]
    working_copy: Mapping[str, object]
    message: str
    recent_messages: tuple[Mapping[str, object], ...]
    context_snippets: tuple[Mapping[str, object], ...]
    attachments: tuple[Mapping[str, object], ...]
    native_images: tuple[Mapping[str, object], ...]
    saved_managed_input: Mapping[str, object] | None
    tools_enabled: bool
    knowledge_search_enabled: bool
    task_intent: str = "auto"
    attachment_overview: Mapping[str, object] | None = None
    attachment_capabilities: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        """Seal nested caller-owned values even when constructed directly."""
        for name in (
            "base_version",
            "available_secret_keys",
            "working_copy",
            "recent_messages",
            "context_snippets",
            "attachments",
            "native_images",
            "saved_managed_input",
            "attachment_overview",
            "attachment_capabilities",
        ):
            object.__setattr__(self, name, _freeze(getattr(self, name)))

    @classmethod
    def capture(
        cls,
        *,
        adapter_id: int,
        language: str,
        system_locale: str,
        base_version: Mapping[str, object] | None,
        available_secret_keys: list[str],
        payload: AiAssistRequest,
        saved_managed_input: dict[str, object] | None,
        parsed_attachments: list[attachments_service.ParsedText] | None,
        native_images: list[attachments_service.NativeImage] | None,
        tools_enabled: bool,
        knowledge_search_enabled: bool,
        attachment_overview: Mapping[str, object] | None = None,
        attachment_capabilities: Mapping[str, object] | None = None,
    ) -> "PromptContext":
        attachment_values = [
            {
                "filename": item.filename,
                "content_type": item.content_type,
                "category": item.category,
                "text": item.text,
                "truncated": item.truncated,
                **(
                    {"text_fields": fields}
                    if item.category == attachments_service.TEXT_CATEGORY
                    and (fields := literal_text_fields(item.text, truncated=item.truncated))
                    else {}
                ),
            }
            for item in parsed_attachments or ()
        ]
        image_values = [
            {"content_type": item.content_type, "data_base64": item.data_base64}
            for item in native_images or ()
        ]
        return cls(
            adapter_id=adapter_id,
            language=language,
            system_locale=system_locale,
            base_version=(
                cast(Mapping[str, object], _freeze(dict(base_version)))
                if base_version is not None
                else None
            ),
            available_secret_keys=tuple(available_secret_keys),
            working_copy=cast(
                Mapping[str, object], _freeze(payload.working_copy.model_dump(mode="json"))
            ),
            message=payload.message,
            task_intent=payload.task_intent,
            attachment_overview=attachment_overview,
            attachment_capabilities=attachment_capabilities,
            recent_messages=tuple(
                cast(Mapping[str, object], _freeze(item.model_dump(mode="json")))
                for item in payload.recent_messages
            ),
            context_snippets=tuple(
                cast(Mapping[str, object], _freeze(item.model_dump(mode="json")))
                for item in payload.context_snippets
            ),
            attachments=tuple(
                cast(Mapping[str, object], _freeze(item)) for item in attachment_values
            ),
            native_images=tuple(cast(Mapping[str, object], _freeze(item)) for item in image_values),
            saved_managed_input=(
                cast(Mapping[str, object], _freeze(saved_managed_input))
                if saved_managed_input is not None
                else None
            ),
            tools_enabled=tools_enabled,
            knowledge_search_enabled=knowledge_search_enabled,
        )
