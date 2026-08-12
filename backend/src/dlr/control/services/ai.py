"""M4 AI setting, context construction and Human-in-the-loop assist service."""

import json
from typing import NoReturn
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from dlr.control.ai import providers
from dlr.control.models import AdapterCredentialBinding, AdapterVersion, AiModelSetting, Credential
from dlr.control.schemas.ai import (
    AiAssistRequest,
    AiAssistResponse,
    AiConnectionTestResponse,
    AiModelOutput,
    AiModelsResponse,
    AiProviderDraft,
    AiSettingDraft,
    AiSettingResponse,
    contains_unicode_surrogate,
)
from dlr.control.services import adapter as adapter_service
from dlr.control.services import secrets as secrets_service
from dlr.control.services.adapter import domain_error

_SINGLETON_ID = 1

_RUNTIME_CONTRACTS = {
    "python": "def handle(context, input):\n    ...",
    "javascript": "export async function handle(context, input) {\n  ...\n}",
    "java": (
        "public class Adapter {\n"
        "    public Object handle(Context context, Object input) throws Exception {\n"
        "        ...\n"
        "    }\n"
        "}"
    ),
}

_PROVIDER_ERRORS: dict[str, tuple[int, str]] = {
    "ai_credential_invalid": (422, "AI API credential is missing, invalid, or not a token"),
    "ai_provider_unreachable": (502, "The AI provider could not be reached"),
    "ai_auth_failed": (502, "The AI provider rejected the configured credential"),
    "ai_model_not_found": (502, "The configured AI model was not found"),
    "ai_timeout": (504, "The AI provider request timed out"),
    "ai_reasoning_unsupported": (
        422,
        "The selected provider or model does not support this reasoning configuration",
    ),
    "ai_response_invalid": (502, "The AI provider returned an invalid response"),
}


def _raise_provider_error(error: providers.AiProviderError) -> NoReturn:
    status_code, message = _PROVIDER_ERRORS.get(error.code, (502, "The AI provider request failed"))
    raise domain_error(status_code, error.code, message) from None


def _resolve_api_key(session: Session, credential_id: int | None) -> str | None:
    """Resolve only token Credentials and never expose their plaintext."""
    if credential_id is None:
        return None
    credential = session.get(Credential, credential_id)
    if credential is None or credential.type != "token":
        raise domain_error(
            422,
            "ai_credential_invalid",
            "AI API credential is missing, invalid, or not a token",
        )
    try:
        token = secrets_service.decrypt_fields(credential.ciphertext).get("token")
    except HTTPException:
        # Secret Store diagnostics stay server-side; the AI API exposes only
        # its stable credential error and never ciphertext/key details.
        raise domain_error(
            422,
            "ai_credential_invalid",
            "AI API credential is missing, invalid, or not a token",
        ) from None
    if not token:
        raise domain_error(
            422,
            "ai_credential_invalid",
            "AI API credential is missing, invalid, or not a token",
        )
    return token


def _validate_reasoning(data: AiSettingDraft) -> None:
    try:
        providers.validate_reasoning(
            providers.get_provider(data.provider), data.reasoning_mode, data.reasoning_effort
        )
    except providers.AiProviderError as error:
        _raise_provider_error(error)


def _validate_base_url(base_url: str) -> None:
    """Validate without reflecting a possibly credential-bearing URL."""
    try:
        if any(
            ord(character) < 0x20 or ord(character) == 0x7F or character.isspace()
            for character in base_url
        ):
            raise ValueError("URL contains whitespace or a control character")
        parts = urlsplit(base_url)
        # Accessing .port performs urllib's invalid/out-of-range port check.
        _ = parts.port
    except ValueError:
        raise domain_error(
            422,
            "ai_base_url_invalid",
            "AI base URL must be an absolute http(s) URL without credentials, query, or fragment",
        ) from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.netloc
        or parts.hostname is None
        or parts.username is not None
        or parts.password is not None
        or bool(parts.query)
        or bool(parts.fragment)
    ):
        raise domain_error(
            422,
            "ai_base_url_invalid",
            "AI base URL must be an absolute http(s) URL without credentials, query, or fragment",
        )


def get_setting(session: Session) -> AiModelSetting | None:
    return session.get(AiModelSetting, _SINGLETON_ID)


def setting_response(session: Session, setting: AiModelSetting) -> AiSettingResponse:
    credential_name = None
    if setting.credential_id is not None:
        credential = session.get(Credential, setting.credential_id)
        credential_name = credential.name if credential is not None else None
    return AiSettingResponse(
        id=setting.id,
        provider=setting.provider,  # type: ignore[arg-type]
        base_url=setting.base_url,
        model=setting.model,
        credential_id=setting.credential_id,
        credential_name=credential_name,
        reasoning_mode=setting.reasoning_mode,  # type: ignore[arg-type]
        reasoning_effort=setting.reasoning_effort,  # type: ignore[arg-type]
        created_at=setting.created_at,
        updated_at=setting.updated_at,
    )


def save_setting(session: Session, data: AiSettingDraft) -> AiModelSetting:
    """Atomically create or replace the singleton configuration."""
    _validate_base_url(data.base_url)
    _validate_reasoning(data)
    _resolve_api_key(session, data.credential_id)
    statement = insert(AiModelSetting).values(
        id=_SINGLETON_ID,
        provider=data.provider,
        base_url=data.base_url,
        model=data.model,
        credential_id=data.credential_id,
        reasoning_mode=data.reasoning_mode,
        reasoning_effort=data.reasoning_effort,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[AiModelSetting.id],
        set_={
            "provider": statement.excluded.provider,
            "base_url": statement.excluded.base_url,
            "model": statement.excluded.model,
            "credential_id": statement.excluded.credential_id,
            "reasoning_mode": statement.excluded.reasoning_mode,
            "reasoning_effort": statement.excluded.reasoning_effort,
            "updated_at": statement.excluded.created_at,
        },
    )
    session.execute(statement)
    session.commit()
    setting = get_setting(session)
    if setting is None:  # defensive: the upsert contract guarantees this row
        raise RuntimeError("AI setting upsert did not create the singleton row")
    session.refresh(setting)
    return setting


def refresh_models(session: Session, data: AiProviderDraft) -> AiModelsResponse:
    _validate_base_url(data.base_url)
    api_key = _resolve_api_key(session, data.credential_id)
    try:
        models = providers.fetch_models(data.provider, data.base_url, api_key)
    except providers.AiProviderError as error:
        _raise_provider_error(error)
    _reject_secret_reflection(models, api_key)
    return AiModelsResponse(models=models)


def test_connection(session: Session, data: AiSettingDraft) -> AiConnectionTestResponse:
    _validate_base_url(data.base_url)
    _validate_reasoning(data)
    api_key = _resolve_api_key(session, data.credential_id)
    messages: list[providers.JsonObject] = [
        {
            "role": "system",
            "content": "This is a connection test. Reply with a short final answer only.",
        },
        {"role": "user", "content": "Reply with OK."},
    ]
    try:
        providers.chat(data, api_key, messages, structured=False)
    except providers.AiProviderError as error:
        _raise_provider_error(error)
    return AiConnectionTestResponse(ok=True, message="Connection successful")


def _setting_draft(setting: AiModelSetting) -> AiSettingDraft:
    return AiSettingDraft(
        provider=setting.provider,  # type: ignore[arg-type]
        base_url=setting.base_url,
        model=setting.model,
        credential_id=setting.credential_id,
        reasoning_mode=setting.reasoning_mode,  # type: ignore[arg-type]
        reasoning_effort=setting.reasoning_effort,  # type: ignore[arg-type]
    )


def _base_version(
    session: Session, adapter_id: int, base_version_id: int | None
) -> dict[str, int] | None:
    if base_version_id is None:
        return None
    version = session.get(AdapterVersion, base_version_id)
    if version is None or version.adapter_id != adapter_id:
        raise domain_error(404, "version_not_found", "Version not found")
    return {"id": version.id, "seq": version.seq}


def _secret_env_keys(session: Session, adapter_id: int) -> list[str]:
    """Read binding names only; this path never joins/decrypts credentials."""
    return list(
        session.scalars(
            select(AdapterCredentialBinding.env_key)
            .where(AdapterCredentialBinding.adapter_id == adapter_id)
            .order_by(AdapterCredentialBinding.env_key.asc())
        ).all()
    )


def _assist_messages(
    session: Session,
    adapter_id: int,
    language: str,
    payload: AiAssistRequest,
) -> list[providers.JsonObject]:
    context = {
        "adapter_id": adapter_id,
        "language": language,
        "base_version": _base_version(session, adapter_id, payload.base_version_id),
        # Names only. Credential rows and ciphertext/plaintext are never read.
        "available_secret_keys": _secret_env_keys(session, adapter_id),
        "working_copy": payload.working_copy.model_dump(mode="json"),
    }
    output_schema = AiModelOutput.model_json_schema()
    system_prompt = (
        "You are the Human-in-the-loop DLR Adapter development assistant.\n"
        "Return exactly one JSON object and no Markdown, prose wrapper, code fence, patch, "
        "tool call, or reasoning. The object must strictly match this JSON Schema:\n"
        f"{json.dumps(output_schema, ensure_ascii=False, sort_keys=True)}\n"
        "A non-null candidate is a complete snapshot. Never include or change language, "
        "published_version_id, production_worker_id, production_state, or any lifecycle action. "
        "Never request, invent, or reveal secret values; use only "
        'context.secrets.get("ENV_KEY") with an available key name.\n'
        f"Runtime Contract for {language}:\n{_RUNTIME_CONTRACTS[language]}\n"
        "Common capabilities: context.config; context.secrets.get(key); context.logger; "
        "JSON-compatible input; JSON-serializable output.\n"
        "The current Working Copy below is the only authoritative code snapshot. Do not infer "
        "code from earlier conversation messages.\n"
        f"Current Adapter context:\n{json.dumps(context, ensure_ascii=False, sort_keys=True)}"
    )
    messages: list[providers.JsonObject] = [{"role": "system", "content": system_prompt}]
    messages.extend(
        {"role": item.role, "content": item.content} for item in payload.recent_messages
    )
    messages.append({"role": "user", "content": payload.message})
    return messages


def _contains_secret(value: object, secret: str) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if secret in item:
                return True
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return False


def _reject_secret_reflection(value: object, api_key: str | None) -> None:
    if api_key and _contains_secret(value, api_key):
        raise domain_error(
            502,
            "ai_response_invalid",
            "The AI provider returned an invalid response",
        )


def _parse_model_output(final_text: str, api_key: str | None = None) -> AiModelOutput:
    _reject_secret_reflection(final_text, api_key)
    try:
        raw = providers.load_json_strict(final_text)
        output = AiModelOutput.model_validate(raw, strict=True)
        visible_output = output.model_dump(mode="json")
        if contains_unicode_surrogate(visible_output):
            raise ValueError("provider output contains an invalid Unicode surrogate")
        _reject_secret_reflection(visible_output, api_key)
        return output
    except (ValueError, ValidationError, RecursionError):
        raise domain_error(
            502,
            "ai_response_invalid",
            "The AI provider returned an invalid response",
        ) from None


def assist(session: Session, adapter_id: int, payload: AiAssistRequest) -> AiAssistResponse:
    """Generate a candidate without writing any DLR lifecycle or version state."""
    adapter = adapter_service.get_adapter(session, adapter_id)
    setting = get_setting(session)
    if setting is None:
        raise domain_error(409, "ai_not_configured", "AI model is not configured")
    draft = _setting_draft(setting)
    _validate_base_url(draft.base_url)
    _validate_reasoning(draft)
    api_key = _resolve_api_key(session, draft.credential_id)
    messages = _assist_messages(session, adapter.id, adapter.language, payload)
    try:
        final_text = providers.chat(draft, api_key, messages, structured=True)
    except providers.AiProviderError as error:
        _raise_provider_error(error)
    output = _parse_model_output(final_text, api_key)
    return AiAssistResponse(
        message=output.message,
        candidate=output.candidate,
        provider=draft.provider,
        model=draft.model,
    )
