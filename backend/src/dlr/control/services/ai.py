"""AI settings, request preparation, runtime invocation and shared terminal audit."""

import asyncio
import json
import time
from collections.abc import Callable
from typing import NoReturn, cast
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.ai import attachments as attachments_service
from dlr.control.ai import context_budget, providers, tool_audit
from dlr.control.ai import knowledge as knowledge_service
from dlr.control.ai import tools as tools_service
from dlr.control.ai.attachment_reading import ScopedAttachmentReader
from dlr.control.ai.diagnostics import AiResponseInvalid
from dlr.control.ai.policy import (
    _FINALIZATION_RESERVE_SECONDS as _FINALIZATION_RESERVE_SECONDS,
)
from dlr.control.ai.policy import (
    _KNOWLEDGE_EXPECTED_TOOL as _KNOWLEDGE_EXPECTED_TOOL,
)
from dlr.control.ai.policy import (
    _MAX_CONSECUTIVE_TOOL_FAILURES as _MAX_CONSECUTIVE_TOOL_FAILURES,
)
from dlr.control.ai.policy import (
    _MAX_KNOWLEDGE_SEARCHES as _MAX_KNOWLEDGE_SEARCHES,
)
from dlr.control.ai.policy import (
    _STOP_ATTACHMENT_UNREAD as _STOP_ATTACHMENT_UNREAD,
)
from dlr.control.ai.policy import (
    _STOP_CALL_BUDGET as _STOP_CALL_BUDGET,
)
from dlr.control.ai.policy import (
    _STOP_CONSECUTIVE_FAILURES as _STOP_CONSECUTIVE_FAILURES,
)
from dlr.control.ai.policy import (
    _STOP_CONTEXT_BUDGET as _STOP_CONTEXT_BUDGET,
)
from dlr.control.ai.policy import (
    _STOP_DEADLINE as _STOP_DEADLINE,
)
from dlr.control.ai.policy import (
    _STOP_DUPLICATE as _STOP_DUPLICATE,
)
from dlr.control.ai.policy import (
    _STOP_KNOWLEDGE_LIST_EMPTY as _STOP_KNOWLEDGE_LIST_EMPTY,
)
from dlr.control.ai.policy import (
    _STOP_KNOWLEDGE_LIST_FAILED as _STOP_KNOWLEDGE_LIST_FAILED,
)
from dlr.control.ai.policy import (
    _STOP_KNOWLEDGE_READ_FAILED as _STOP_KNOWLEDGE_READ_FAILED,
)
from dlr.control.ai.policy import (
    _STOP_KNOWLEDGE_READY as _STOP_KNOWLEDGE_READY,
)
from dlr.control.ai.policy import (
    _STOP_KNOWLEDGE_SEARCH_EMPTY as _STOP_KNOWLEDGE_SEARCH_EMPTY,
)
from dlr.control.ai.policy import (
    _STOP_KNOWLEDGE_SEARCH_FAILED as _STOP_KNOWLEDGE_SEARCH_FAILED,
)
from dlr.control.ai.policy import (
    _STOP_KNOWLEDGE_SEQUENCE as _STOP_KNOWLEDGE_SEQUENCE,
)
from dlr.control.ai.policy import (
    _STOP_KNOWLEDGE_UNAVAILABLE as _STOP_KNOWLEDGE_UNAVAILABLE,
)
from dlr.control.ai.policy import (
    _STOP_PROVIDER_FAILURE as _STOP_PROVIDER_FAILURE,
)
from dlr.control.ai.policy import (
    _STOP_RESULT_BUDGET as _STOP_RESULT_BUDGET,
)
from dlr.control.ai.policy import (
    _STOP_ROUND_BUDGET as _STOP_ROUND_BUDGET,
)
from dlr.control.ai.policy import (
    _AssistToolState as _AssistToolState,
)
from dlr.control.ai.policy import (
    _KnowledgeRetrievalState as _KnowledgeRetrievalState,
)
from dlr.control.ai.policy import (
    _parse_model_output as _parse_model_output,
)
from dlr.control.ai.policy import (
    _reject_candidate_configuration_changes as _reject_candidate_configuration_changes,
)
from dlr.control.ai.policy import (
    _reject_secret_reflection as _reject_secret_reflection,
)
from dlr.control.ai.policy import (
    complete_response,
)
from dlr.control.ai.presentation import (
    _assist_response as _assist_response,
)
from dlr.control.ai.presentation import (
    _fallback_assist_response as _fallback_assist_response,
)
from dlr.control.ai.presentation import (
    _fallback_message as _fallback_message,
)
from dlr.control.ai.presentation import (
    _knowledge_evidence_message as _knowledge_evidence_message,
)
from dlr.control.ai.presentation import (
    _tool_call_summary as _tool_call_summary,
)
from dlr.control.ai.presentation import (
    _transparent_knowledge_message as _transparent_knowledge_message,
)
from dlr.control.ai.presentation import (
    _with_omission_note as _with_omission_note,
)
from dlr.control.ai.prompt_builder import build_prompt
from dlr.control.ai.prompt_context import PromptContext
from dlr.control.ai.provider_config import ProviderSnapshot
from dlr.control.ai.runtime.contracts import AssistExecution, PreparedAssist, RuntimeResources
from dlr.control.ai.runtime.legacy import LegacyRuntime
from dlr.control.ai.runtime.resources import ReaderFactory, ToolResourceFactory
from dlr.control.models import (
    AdapterCredentialBinding,
    AdapterInputArtifactBinding,
    AdapterInputConfig,
    AdapterVersion,
    AiCustomProvider,
    AiModelSetting,
    Credential,
    ManagedInputArtifact,
)
from dlr.control.schemas.ai import (
    AiAssistRequest,
    AiAssistResponse,
    AiAttachmentCapabilitiesResponse,
    AiAttachmentLimits,
    AiConnectionTestResponse,
    AiCustomProviderDraft,
    AiCustomProviderResponse,
    AiCustomProvidersResponse,
    AiCustomProviderTestRequest,
    AiKnowledgeCapabilityResponse,
    AiModelOutput,
    AiModelsResponse,
    AiProviderAttachmentCapability,
    AiProviderCapability,
    AiProviderDraft,
    AiProvidersResponse,
    AiSelectedAttachmentCapability,
    AiSettingDraft,
    AiSettingResponse,
    AiToolCallSummary,
)
from dlr.control.services import adapter as adapter_service
from dlr.control.services import knowledge_source as knowledge_source_service_config
from dlr.control.services import locale as locale_service
from dlr.control.services import secrets as secrets_service
from dlr.control.services.adapter import domain_error

_SINGLETON_ID = 1


_RUNTIME_CONTRACTS = {
    "python": "def handle(context, input):\n    ...",
    "javascript": "export async function handle(context, input) {\n  ...\n}",
    "typescript": (
        "export async function handle(context: Context, input: unknown) { ... }\n"
        "Strict TypeScript 5.8.3 / Node ESM. DLR.Context or import type { Context } from 'dlr'. "
        "Narrow unknown inputs or declare input interfaces. npm dependencies use package@version."
    ),
    "go": (
        "package main\nfunc Handle(ctx *Context, input any) (any, error) { ... }\n"
        "Context embeds context.Context. Use ctx.Config, ctx.Secrets.Get(key) (empty if absent), "
        "ctx.Logger.Info/Warn/Error, ctx.InputFiles. DecodeInput(input, &value) decodes structs. "
        "Dependencies use module/path@vX.Y.Z. Pure Go only; no main function or CGO. "
        "Return a JSON-serializable value and nil, or nil and an error."
    ),
    "java": (
        "public class Adapter {\n"
        "    public Object handle(Context context, Object input) throws Exception {\n"
        "        ...\n"
        "    }\n"
        "}"
    ),
}

_PROVIDER_ERRORS: dict[str, tuple[int, str]] = {
    "ai_context_over_budget": (413, "AI request exceeds the model or transport budget"),
    "ai_credential_invalid": (
        422,
        "凭据无效：所选凭据不存在或不是 token 类型，请检查凭据配置后重试",
    ),
    "ai_provider_unreachable": (
        502,
        "无法连接模型服务，请检查网络连通性、防火墙与代理设置后重试",
    ),
    "ai_provider_http_error": (
        502,
        "模型服务返回 HTTP 错误，请检查服务状态或请求配置",
    ),
    "ai_provider_dns_failed": (
        502,
        "模型服务域名解析失败：容器无法把域名解析为 IP 地址，请在容器内检查 DNS 配置"
        "（企业网络 / VPN 下可参考 README「容器网络与 DNS 排障」）后重试",
    ),
    "ai_auth_failed": (
        502,
        "模型服务拒绝了所选凭据：请检查 API Key 是否正确、有效且属于当前服务商",
    ),
    "ai_model_not_found": (502, "模型 ID 不存在：请检查模型 ID 是否正确，或改用其他可用模型 ID"),
    "ai_timeout": (504, "模型服务请求超时：请检查网络连接，稍后重试"),
    "ai_reasoning_unsupported": (
        422,
        "所选服务商或模型不支持该推理配置：请调整推理策略，或改用支持推理的模型",
    ),
    "ai_response_invalid": (
        502,
        "模型服务返回了无法解析的响应：请确认该服务兼容 OpenAI 接口后重试",
    ),
    "ai_models_not_supported": (
        502,
        "无法自动获取模型列表：该服务未提供兼容的模型列表接口，可手工填写模型 ID",
    ),
    # M5.7 Wave C1: stable, actionable Tool Call errors. Messages never echo
    # tool arguments, results or any Secret.
    "ai_tool_unsupported": (
        422,
        "当前模型服务不支持受控只读工具调用：请更换支持工具调用的服务商，"
        "或在不使用工具的情况下重试",
    ),
    "ai_tool_limit_exceeded": (
        502,
        f"AI 工具调用达到安全上限（单次最多 {tools_service.MAX_TOOL_CALLS_PER_ASSIST} 次调用 / "
        f"{tools_service.MAX_TOOL_ROUNDS} 轮）：已安全停止，请简化问题后重试",
    ),
    "ai_tool_result_too_large": (
        502,
        "AI 工具结果累计超过大小上限：已安全停止，请缩小查询范围后重试",
    ),
    "ai_knowledge_unavailable": (
        409,
        "知识库检索当前不可用：请确认管理员已启用并配置可用的知识源",
    ),
    "ai_knowledge_disabled": (
        422,
        "本轮未启用知识库检索：请通过对话框中的开关重新发送",
    ),
    "ai_custom_provider_not_found": (404, "自定义模型服务不存在，请刷新设置后重试"),
    "ai_custom_provider_referenced": (
        409,
        "自定义模型服务仍被当前 AI 设置引用，请先切换模型服务后再删除",
    ),
}

# M5.7 Wave B2 canonical zh attachment messages (frontend localizes by the
# stable error code; the message field stays a zh-CN compatibility fallback,
# exactly like _PROVIDER_ERRORS). Errors never echo file content, filenames,
# base64 bodies or Secrets.
_ATTACHMENT_ERROR_MESSAGES: dict[str, str] = {
    "ai_attachment_invalid": "附件数据无效：请重新上传文件",
    "ai_attachment_filename_invalid": "附件文件名无效：请使用不含路径分隔符的普通文件名",
    "ai_attachment_type_unsupported": (
        "附件类型不支持：仅支持 PNG / JPEG / WebP 图片、PDF、DOCX、XLS / XLSX 与文本 / 代码文件，"
        "且文件扩展名必须与声明的类型一致"
    ),
    "ai_attachment_too_large": (
        f"附件超过单文件大小上限（{attachments_service.MAX_FILE_BYTES // 1024 // 1024} MiB）："
        "请压缩或拆分后重新上传"
    ),
    "ai_attachment_total_too_large": (
        f"附件总大小超过上限（{attachments_service.MAX_TOTAL_BYTES // 1024 // 1024} MiB）："
        "请减少或压缩附件后重新上传"
    ),
    "ai_attachment_count_exceeded": (
        f"附件数量超过上限（{attachments_service.MAX_ATTACHMENTS} 个）：请减少附件数量后重试"
    ),
    "ai_attachment_image_unsupported": (
        "当前模型不支持图片输入：请更换支持图片的模型后再发送图片附件"
        "（DLR 不会对图片进行 OCR 并伪装为模型看图）"
    ),
    "ai_attachment_parse_failed": "附件解析失败：文件已损坏、加密或格式不兼容，请重新导出后上传",
    "ai_attachment_no_text": (
        "文档中没有可提取的文本层（可能是扫描件）：请提供带文本层的 PDF / DOCX / XLS / XLSX，"
        "或更换支持图片 / 原生文件的模型"
    ),
    "ai_attachment_unsafe_archive": "附件内容不安全：压缩包结构或解压比例超出允许范围",
    "ai_attachment_parse_timeout": "附件解析超时：文件结构过于复杂，请尝试缩小或简化文件后重试",
}


def _raise_provider_error(error: providers.AiProviderError) -> NoReturn:
    status_code, message = _PROVIDER_ERRORS.get(error.code, (502, "The AI provider request failed"))
    if error.code == "ai_response_invalid":
        raise AiResponseInvalid(error.stage, error.reason, message) from None
    params = (
        {"http_status": error.http_status}
        if error.code == "ai_provider_http_error" and error.http_status is not None
        else None
    )
    raise domain_error(status_code, error.code, message, params) from None


def _raise_tool_error(code: str) -> NoReturn:
    """Stable Tool Call error through the same zh compat-message table as the
    provider errors (the frontend localizes by the stable code; the message
    field stays a zh-CN compatibility fallback by design)."""
    status_code, message = _PROVIDER_ERRORS.get(code, (502, "The AI tool request failed"))
    raise domain_error(status_code, code, message) from None


def _raise_attachment_error(error: attachments_service.AttachmentError) -> NoReturn:
    status_code = attachments_service.ATTACHMENT_ERROR_STATUS.get(error.code, 422)
    message = _ATTACHMENT_ERROR_MESSAGES.get(error.code, "附件处理失败：请检查文件后重试")
    raise domain_error(status_code, error.code, message) from None


def _audit_error_code(error: BaseException) -> str:
    """Extract only a stable code for the request-terminal audit event."""

    if isinstance(error, providers.AiProviderError):
        return error.code
    if isinstance(error, asyncio.CancelledError):
        return "ai_assist_cancelled"
    if isinstance(error, HTTPException) and isinstance(error.detail, dict):
        code = error.detail.get("code")
        if isinstance(code, str):
            return code
    return "ai_assist_failed"


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


def _validate_reasoning(
    data: AiSettingDraft, adapter: providers.ProviderAdapter | None = None
) -> None:
    try:
        providers.validate_reasoning(
            adapter or providers.get_provider(data.provider),
            data.reasoning_mode,
            data.reasoning_effort,
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


def _custom_provider(session: Session, provider_id: int) -> AiCustomProvider:
    provider = session.get(AiCustomProvider, provider_id)
    if provider is None:
        raise domain_error(
            404,
            "ai_custom_provider_not_found",
            "Custom AI provider not found",
        )
    return provider


def _adapter_for_setting(session: Session, data: AiSettingDraft) -> providers.ProviderAdapter:
    if data.provider != "custom_openai_compatible" or data.custom_provider_id is None:
        if data.custom_provider_id is not None:
            raise domain_error(
                422, "ai_custom_provider_invalid", "Custom provider reference invalid"
            )
        return providers.get_provider(data.provider)
    custom = _custom_provider(session, data.custom_provider_id)
    return providers.custom_provider_adapter(
        custom.protocol,  # type: ignore[arg-type]
        images_native=custom.images_native,
        files_native=custom.files_native,
        tools_supported=custom.tools_supported,
    )


def _validate_setting(
    session: Session, data: AiSettingDraft, *, resolve_credentials: bool = True
) -> providers.ProviderAdapter:
    _validate_base_url(data.base_url)
    adapter = providers.resolve_attachment_adapter(
        data, providers.resolve_output_adapter(data, _adapter_for_setting(session, data))
    )
    _validate_reasoning(data, adapter)
    if resolve_credentials:
        _resolve_api_key(session, data.credential_id)
    return adapter


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
        custom_provider_id=setting.custom_provider_id,
        credential_name=credential_name,
        reasoning_mode=setting.reasoning_mode,  # type: ignore[arg-type]
        reasoning_effort=setting.reasoning_effort,  # type: ignore[arg-type]
        created_at=setting.created_at,
        updated_at=setting.updated_at,
    )


def save_setting(session: Session, data: AiSettingDraft) -> AiModelSetting:
    """Atomically create or replace the singleton configuration."""
    _validate_setting(session, data)
    normalized_base_url = providers.normalize_base_url(data.base_url)
    statement = insert(AiModelSetting).values(
        id=_SINGLETON_ID,
        provider=data.provider,
        base_url=normalized_base_url,
        model=data.model,
        credential_id=data.credential_id,
        custom_provider_id=data.custom_provider_id,
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
            "custom_provider_id": statement.excluded.custom_provider_id,
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
    setting_data = AiSettingDraft(
        provider=data.provider,
        base_url=data.base_url,
        model="manual-model",
        credential_id=data.credential_id,
        custom_provider_id=data.custom_provider_id,
    )
    adapter = _adapter_for_setting(session, setting_data)
    api_key = _resolve_api_key(session, data.credential_id)
    try:
        models = providers.fetch_models(data.provider, data.base_url, api_key, adapter)
    except providers.AiProviderError as error:
        _raise_provider_error(error)
    _reject_secret_reflection(models, api_key)
    return AiModelsResponse(models=models)


def test_connection(session: Session, data: AiSettingDraft) -> AiConnectionTestResponse:
    adapter = _validate_setting(session, data)
    api_key = _resolve_api_key(session, data.credential_id)
    messages: list[providers.JsonObject] = [
        {
            "role": "system",
            "content": "This is a connection test. Reply with a short final answer only.",
        },
        {"role": "user", "content": "Reply with OK."},
    ]
    try:
        providers.chat(data, api_key, messages, structured=False, adapter=adapter)
    except providers.AiProviderError as error:
        _raise_provider_error(error)
    return AiConnectionTestResponse(ok=True, message="模型服务返回了可解析的最小响应")


def _setting_draft(setting: AiModelSetting) -> AiSettingDraft:
    return AiSettingDraft(
        provider=setting.provider,  # type: ignore[arg-type]
        base_url=setting.base_url,
        model=setting.model,
        credential_id=setting.credential_id,
        custom_provider_id=setting.custom_provider_id,
        reasoning_mode=setting.reasoning_mode,  # type: ignore[arg-type]
        reasoning_effort=setting.reasoning_effort,  # type: ignore[arg-type]
    )


def provider_catalog() -> AiProvidersResponse:
    """Return the fixed preset catalog and explicit protocol capabilities."""
    return AiProvidersResponse(
        providers=[
            AiProviderCapability(
                id=provider,
                name=providers.PROVIDER_DISPLAY_NAMES[provider],
                preset=True,
                protocol=adapter.protocol,
                base_url=providers.PROVIDER_DEFAULT_BASE_URLS[provider],
                images_native=adapter.images_native,
                files_native=adapter.files_native,
                tools_supported=adapter.tools_supported,
                reasoning_efforts=sorted(adapter.reasoning_efforts),
            )
            for provider, adapter in providers.PROVIDERS.items()
        ]
    )


def _custom_provider_response(
    session: Session, provider: AiCustomProvider
) -> AiCustomProviderResponse:
    credential_name = None
    if provider.credential_id is not None:
        credential = session.get(Credential, provider.credential_id)
        credential_name = credential.name if credential is not None else None
    referenced = (
        session.scalar(
            select(AiModelSetting.id).where(AiModelSetting.custom_provider_id == provider.id)
        )
        is not None
    )
    return AiCustomProviderResponse(
        id=provider.id,
        name=provider.name,
        protocol=provider.protocol,  # type: ignore[arg-type]
        base_url=provider.base_url,
        credential_id=provider.credential_id,
        credential_name=credential_name,
        images_native=provider.images_native,
        files_native=provider.files_native,
        tools_supported=provider.tools_supported,
        referenced=referenced,
        created_at=provider.created_at,
        updated_at=provider.updated_at,
    )


def list_custom_providers(session: Session) -> AiCustomProvidersResponse:
    rows = session.scalars(select(AiCustomProvider).order_by(AiCustomProvider.name.asc())).all()
    return AiCustomProvidersResponse(
        providers=[_custom_provider_response(session, row) for row in rows]
    )


def _validate_custom_provider(session: Session, data: AiCustomProviderDraft) -> None:
    _validate_base_url(data.base_url)
    _resolve_api_key(session, data.credential_id)


def create_custom_provider(
    session: Session, data: AiCustomProviderDraft
) -> AiCustomProviderResponse:
    _validate_custom_provider(session, data)
    duplicate = session.scalar(
        select(AiCustomProvider.id).where(AiCustomProvider.name == data.name)
    )
    if duplicate is not None:
        raise domain_error(
            409, "ai_custom_provider_name_taken", "Custom provider name is already used"
        )
    provider = AiCustomProvider(**data.model_dump())
    session.add(provider)
    session.commit()
    session.refresh(provider)
    return _custom_provider_response(session, provider)


def update_custom_provider(
    session: Session, provider_id: int, data: AiCustomProviderDraft
) -> AiCustomProviderResponse:
    provider = _custom_provider(session, provider_id)
    _validate_custom_provider(session, data)
    duplicate = session.scalar(
        select(AiCustomProvider.id).where(
            AiCustomProvider.name == data.name, AiCustomProvider.id != provider_id
        )
    )
    if duplicate is not None:
        raise domain_error(
            409, "ai_custom_provider_name_taken", "Custom provider name is already used"
        )
    for key, value in data.model_dump().items():
        setattr(provider, key, value)
    session.commit()
    session.refresh(provider)
    return _custom_provider_response(session, provider)


def delete_custom_provider(session: Session, provider_id: int) -> None:
    provider = _custom_provider(session, provider_id)
    if (
        session.scalar(
            select(AiModelSetting.id).where(AiModelSetting.custom_provider_id == provider_id)
        )
        is not None
    ):
        raise domain_error(
            409,
            "ai_custom_provider_referenced",
            "Custom provider is referenced by the active AI setting",
        )
    session.delete(provider)
    session.commit()


def test_custom_provider(
    session: Session, provider_id: int, data: AiCustomProviderTestRequest
) -> AiConnectionTestResponse:
    provider = _custom_provider(session, provider_id)
    draft = AiSettingDraft(
        provider="custom_openai_compatible",
        custom_provider_id=provider.id,
        base_url=provider.base_url,
        model=data.model,
        credential_id=provider.credential_id,
    )
    adapter = _validate_setting(session, draft)
    api_key = _resolve_api_key(session, provider.credential_id)
    try:
        providers.chat(
            draft,
            api_key,
            [
                {"role": "system", "content": "This is a connection test. Reply with OK."},
                {"role": "user", "content": "Reply with OK."},
            ],
            structured=False,
            adapter=adapter,
        )
    except providers.AiProviderError as error:
        _raise_provider_error(error)
    return AiConnectionTestResponse(ok=True, message="模型服务返回了可解析的最小响应")


def knowledge_capability(session: Session) -> AiKnowledgeCapabilityResponse:
    available, reason = knowledge_source_service_config.knowledge_search_capability(session)
    return AiKnowledgeCapabilityResponse(available=available, reason=reason)


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


def _saved_managed_input_context(session: Session, adapter_id: int) -> dict[str, object] | None:
    """Read the saved input labels without touching Blobs or lifecycle state.

    Assist only needs the source plus the current ordered Binding labels.  Keep
    this as one narrow, non-locking projection so AI cannot accidentally join
    the ArtifactStore/Lease path used by Execution creation.
    """
    rows = session.execute(
        select(
            AdapterInputConfig.source_type,
            AdapterInputArtifactBinding.ordinal,
            ManagedInputArtifact.original_filename,
            ManagedInputArtifact.content_type,
        )
        .select_from(AdapterInputConfig)
        .outerjoin(
            AdapterInputArtifactBinding,
            (AdapterInputArtifactBinding.adapter_id == AdapterInputConfig.adapter_id)
            & (AdapterInputArtifactBinding.input_config_revision == AdapterInputConfig.revision),
        )
        .outerjoin(
            ManagedInputArtifact,
            (ManagedInputArtifact.id == AdapterInputArtifactBinding.artifact_id)
            & (ManagedInputArtifact.adapter_id == AdapterInputArtifactBinding.adapter_id),
        )
        .where(AdapterInputConfig.adapter_id == adapter_id)
        .order_by(AdapterInputArtifactBinding.ordinal.asc())
    ).all()
    if not rows:
        return None

    source_type = rows[0][0]
    if source_type == "remote_files":
        return {"source_type": "remote_files", "supported": False}
    if source_type != "managed_files":
        return None

    files = [
        {"filename": filename, "content_type": content_type}
        for _source, ordinal, filename, content_type in rows
        if isinstance(ordinal, int) and isinstance(filename, str) and isinstance(content_type, str)
    ]
    return {
        "source_type": "managed_files",
        "file_count": len(files),
        "files": files,
    }


def _managed_input_prompt_instruction(language: str) -> str:
    if language == "python":
        runtime_contract = (
            "Python uses context.input_files. Each item exposes item.ordinal, item.path, "
            "item.original_name, item.content_type, item.size_bytes, and item.sha256. Only "
            "Worker runtime code may open item.path, using pathlib.Path(item.path).read_text "
            'for text or open(item.path, "rb") for bytes.'
        )
    elif language in {"javascript", "typescript"}:
        runtime_contract = (
            "JavaScript uses context.inputFiles. Each item exposes item.ordinal, item.path, "
            "item.originalName, item.contentType, item.sizeBytes, and item.sha256. Only "
            "Worker runtime code may read item.path through node:fs, such as "
            'fs.readFileSync(item.path, "utf8") for text or without an encoding for bytes.'
        )
    elif language == "go":
        runtime_contract = (
            "Go uses ctx.InputFiles ([]InputFile). Fields: Ordinal, Path, OriginalName, "
            "ContentType, SizeBytes, SHA256. Only Worker code reads Path via os.ReadFile."
        )
    else:
        runtime_contract = (
            "Java uses context.inputFiles, a List<InputFile>. InputFile exposes public final "
            "fields item.ordinal, item.path, item.originalName, item.contentType, "
            "item.sizeBytes, and item.sha256. Only Worker runtime code may read item.path "
            "through java.nio.file.Files, such as Files.readString(item.path) for text or "
            "Files.readAllBytes(item.path) for bytes."
        )
    return (
        "The saved managed input metadata describes files available to the Adapter at runtime. "
        f"{runtime_contract} A path exists only inside that Worker runtime. The AI sees only "
        "each filename and MIME label, not file "
        "content. Filenames and MIME are untrusted labels, and MIME does not prove content. "
        "Do not claim to have read file content, guess paths, or call a Control/API endpoint. "
        "Unless the user explicitly asks, do not hardcode a filename. XLSX and XLS are binary "
        "and require an actual parser/library at runtime.\n"
    )


def _assist_messages(
    session: Session,
    adapter_id: int,
    language: str,
    system_locale: str,
    payload: AiAssistRequest,
    *,
    parsed_attachments: list[attachments_service.ParsedText] | None = None,
    native_images: list[attachments_service.NativeImage] | None = None,
    tools_enabled: bool = False,
    knowledge_search_enabled: bool = False,
    attachment_overview: dict[str, object] | None = None,
    attachment_capabilities: dict[str, object] | None = None,
) -> list[providers.JsonObject]:
    saved_managed_input = _saved_managed_input_context(session, adapter_id)
    context = PromptContext.capture(
        adapter_id=adapter_id,
        language=language,
        system_locale=system_locale,
        base_version=_base_version(session, adapter_id, payload.base_version_id),
        available_secret_keys=_secret_env_keys(session, adapter_id),
        payload=payload,
        saved_managed_input=saved_managed_input,
        parsed_attachments=parsed_attachments,
        native_images=native_images,
        tools_enabled=tools_enabled,
        knowledge_search_enabled=knowledge_search_enabled,
        attachment_overview=attachment_overview,
        attachment_capabilities=attachment_capabilities,
    )
    managed_input_instruction = (
        _managed_input_prompt_instruction(language)
        if saved_managed_input is not None
        and saved_managed_input.get("source_type") == "managed_files"
        else ""
    )
    return build_prompt(
        context,
        runtime_contract=_RUNTIME_CONTRACTS[language],
        managed_input_instruction=managed_input_instruction,
    ).provider_messages()


def attachment_capabilities(session: Session | None = None) -> AiAttachmentCapabilitiesResponse:
    """Stable Wave B3 contract: limits, accepted MIME types and the explicit
    per-Provider native-attachment capability table."""
    selected = None
    setting = get_setting(session) if session is not None else None
    if setting is not None and session is not None:
        draft = _setting_draft(setting)
        adapter = providers.resolve_attachment_adapter(draft, _adapter_for_setting(session, draft))
        selected = AiSelectedAttachmentCapability(
            provider=draft.provider,
            model=draft.model,
            protocol=adapter.protocol,
            images_native=adapter.images_native,
            native_pdf=adapter.native_pdf,
            capability_source=adapter.capability_source,
            max_request_bytes=adapter.max_request_bytes,
        )
    return AiAttachmentCapabilitiesResponse(
        selected=selected,
        prepared_attachments_enabled=bool(settings.ai_document_converter_url),
        limits=AiAttachmentLimits(
            max_attachments=attachments_service.MAX_ATTACHMENTS,
            max_file_bytes=attachments_service.MAX_FILE_BYTES,
            max_total_bytes=attachments_service.MAX_TOTAL_BYTES,
            max_parsed_chars_per_file=attachments_service.MAX_PARSED_CHARS_PER_FILE,
            max_parsed_total_chars=attachments_service.MAX_PARSED_TOTAL_CHARS,
            parse_timeout_seconds=attachments_service.PARSE_TIMEOUT_SECONDS,
        ),
        supported_content_types=attachments_service.supported_content_types(),
        providers=[
            AiProviderAttachmentCapability(
                provider=adapter.provider,
                images_native=adapter.images_native,
                files_native=adapter.files_native,
            )
            for adapter in providers.PROVIDERS.values()
        ],
    )


def _assistant_tool_call(
    call: providers.NormalizedToolCall,
    api_key: str | None,
    tool_context: tools_service.ToolExecutionContext,
) -> providers.JsonObject:
    return {
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.name,
            # Only this sanitized echo rejoins the Provider conversation.
            "arguments": tools_service.sanitize_text(
                call.arguments,
                api_key,
                4000,
                extra_values=tuple(tool_context.secret_values),
            ),
        },
    }


def _audit_provider_budget(
    audit: tool_audit.AiToolAuditTrail | None,
    state: _AssistToolState,
    budget: context_budget.BudgetResult,
    provider_deadline: float,
) -> None:
    if audit is None:
        return
    now = time.monotonic()
    audit.record_provider_budget(
        budget,
        elapsed_ms=int((now - state.started_at) * 1000),
        provider_deadline_ms=int((provider_deadline - state.started_at) * 1000),
        hard_deadline_ms=int((state.hard_deadline - state.started_at) * 1000),
        remaining_ms=int(max(0.0, provider_deadline - now) * 1000),
    )


def _call_provider_with_audit(
    draft: AiSettingDraft,
    api_key: str | None,
    messages: list[providers.JsonObject],
    *,
    tools: list[providers.JsonObject] | None,
    provider_adapter: providers.ProviderAdapter,
    timeout_seconds: float,
    state: _AssistToolState,
    audit: tool_audit.AiToolAuditTrail | None,
) -> tuple[str | None, list[providers.NormalizedToolCall] | None]:
    started_at = time.monotonic() if audit is not None else 0.0
    try:
        result = providers.chat_assist(
            draft,
            api_key,
            messages,
            tools=tools,
            image_input=context_budget.has_native_image(messages),
            adapter=provider_adapter,
            timeout_seconds=min(timeout_seconds, state.remaining_total_seconds()),
            hard_deadline=state.hard_deadline,
        )
        if state.remaining_total_seconds() <= 0:
            raise providers.AiProviderError("ai_timeout")
    except providers.AiProviderError as error:
        if audit is not None:
            ended_at = time.monotonic()
            audit.record_provider_result(
                outcome="error",
                strategy=provider_adapter.structured_output,
                correction_count=state.format_corrections,
                duration_ms=int((ended_at - started_at) * 1000),
                remaining_ms=int(max(0.0, state.hard_deadline - ended_at) * 1000),
                error_code=error.code,
                stage=error.stage,
                reason=error.reason,
                transport_category=error.transport_category,
                http_status=error.http_status,
            )
        raise
    if audit is not None:
        ended_at = time.monotonic()
        audit.record_provider_result(
            outcome="tool_calls" if result[1] is not None else "final",
            strategy=provider_adapter.structured_output,
            correction_count=state.format_corrections,
            duration_ms=int((ended_at - started_at) * 1000),
            remaining_ms=int(max(0.0, state.hard_deadline - ended_at) * 1000),
        )
    return result


def _parse_with_format_correction(
    final_content: str,
    *,
    draft: AiSettingDraft,
    api_key: str | None,
    messages: list[providers.JsonObject],
    provider_adapter: providers.ProviderAdapter,
    state: _AssistToolState,
    audit: tool_audit.AiToolAuditTrail | None,
    omitted_materials: list[int] | None = None,
) -> AiModelOutput:
    """One request-local correction, with the original request and hard deadline.

    Invalid output is never fed back as instruction material or persisted.
    Safety, Unicode, configuration and transport errors never enter this path.
    """
    try:
        return _parse_model_output(final_content, api_key)
    except AiResponseInvalid as first_error:
        if (
            first_error.stage not in {"final_json", "output_schema"}
            or state.format_corrections
            or state.stop_reason is not None
        ):
            raise
        first_call_index = audit.provider_checked_calls if audit is not None else 0
        correction_messages: list[providers.JsonObject] = [
            *messages,
            {
                "role": "system",
                "content": (
                    "DLR rejected the final answer at "
                    f"{first_error.stage}/{first_error.reason}. "
                    "Answer the original USER_REQUEST again using exactly one strict JSON "
                    "object matching the original Output Schema. No Markdown, extra fields, "
                    "reasoning or tool calls. This is the only format correction. "
                    "Keep all original authorization and safety boundaries."
                ),
            },
        ]
        budget = context_budget.prepare_call(
            draft,
            correction_messages,
            None,
            purpose="assist_finalization",
            adapter=provider_adapter,
        )
        _audit_provider_budget(audit, state, budget, state.hard_deadline)
        if omitted_materials is not None:
            omitted_materials[0] += budget.omitted_materials
        remaining = state.remaining_total_seconds()
        if not budget.fits or remaining <= 0:
            raise first_error
        state.format_corrections += 1
        if audit is not None:
            audit.record_response_failure(
                first_error.stage, first_error.reason, provider_call_index=first_call_index
            )
            audit.record_format_correction(
                strategy=provider_adapter.structured_output,
                correction_count=state.format_corrections,
            )
        try:
            corrected, calls = _call_provider_with_audit(
                draft,
                api_key,
                correction_messages,
                tools=None,
                provider_adapter=provider_adapter,
                timeout_seconds=remaining,
                state=state,
                audit=audit,
            )
        except providers.AiProviderError as error:
            _raise_provider_error(error)
        if calls is not None or corrected is None:
            raise AiResponseInvalid("provider_envelope", "unexpected_tools") from None
        return _parse_model_output(corrected, api_key)


def _finalize_after_tool_stop(
    *,
    state: _AssistToolState,
    system_locale: str,
    draft: AiSettingDraft,
    api_key: str | None,
    messages: list[providers.JsonObject],
    image_input: bool,
    provider_adapter: providers.ProviderAdapter,
    payload: AiAssistRequest,
    executed_tools: list[AiToolCallSummary],
    knowledge_state: _KnowledgeRetrievalState | None = None,
    omitted_materials: list[int] | None = None,
    audit: tool_audit.AiToolAuditTrail | None = None,
) -> AiAssistResponse:
    """Attempt exactly one tools-disabled final answer, then fail closed.

    The control message contains only stable counters and the stop reason. The
    already-sanitized tool messages remain available in memory, while raw
    Provider responses and prompts are never persisted by this path.
    """
    assert state.stop_reason is not None
    if state.attachment_read_required and not state.successful_attachment_reads:
        # A successful directory/list/knowledge tool is not attachment content
        # evidence. Never ask a tools-disabled finalizer to fill this gap.
        return _fallback_assist_response(
            system_locale, _STOP_ATTACHMENT_UNREAD, draft, executed_tools, state=state
        )
    successful = sum(item.status == "success" for item in executed_tools)
    if successful == 0:
        # With no usable tool result there is no evidence from which a safe
        # Candidate can be produced. A transparent server-owned response is
        # stronger than asking the model to guess after repeated failures.
        return _fallback_assist_response(
            system_locale, state.stop_reason, draft, executed_tools, state=state
        )
    remaining = state.remaining_total_seconds()
    if remaining <= 0:
        return _fallback_assist_response(
            system_locale, state.stop_reason, draft, executed_tools, state=state
        )
    messages.append(
        {
            "role": "system",
            "content": (
                "DLR stopped tool execution at a safety boundary "
                f"({state.stop_reason}). Use only the sanitized tool results already in "
                f"this conversation ({successful} successful of {len(executed_tools)} recorded). "
                "Do not request any tool. Return one strict AiModelOutput JSON object and "
                "state what remains unconfirmed."
            ),
        }
    )
    if knowledge_state is not None and knowledge_state.phase != "disabled":
        messages.append(
            {
                "role": "system",
                "content": knowledge_state.finalization_instruction(system_locale),
            }
        )
    budget = context_budget.prepare_call(
        draft, messages, None, purpose="assist_finalization", adapter=provider_adapter
    )
    _audit_provider_budget(audit, state, budget, state.hard_deadline)
    if omitted_materials is not None:
        omitted_materials[0] += budget.omitted_materials
    if not budget.fits:
        return _fallback_assist_response(
            system_locale, _STOP_CONTEXT_BUDGET, draft, executed_tools, state=state
        )
    remaining = state.remaining_total_seconds()
    if remaining <= 0:
        return _fallback_assist_response(
            system_locale, _STOP_DEADLINE, draft, executed_tools, state=state
        )
    try:
        final_content, tool_calls = _call_provider_with_audit(
            draft,
            api_key,
            messages,
            tools=None,
            provider_adapter=provider_adapter,
            state=state,
            audit=audit,
            timeout_seconds=remaining,
        )
        if tool_calls is not None or final_content is None:
            raise AiResponseInvalid("provider_envelope", "unexpected_tools")
        output = _parse_with_format_correction(
            final_content,
            draft=draft,
            api_key=api_key,
            messages=messages,
            provider_adapter=provider_adapter,
            state=state,
            audit=audit,
            omitted_materials=omitted_materials,
        )
        _reject_candidate_configuration_changes(output, payload)
    except (providers.AiProviderError, HTTPException, ValueError) as error:
        if audit is not None and (
            isinstance(error, AiResponseInvalid)
            or isinstance(error, providers.AiProviderError)
            and error.code == "ai_response_invalid"
        ):
            audit.record_response_failure(error.stage, error.reason)
        return _fallback_assist_response(
            system_locale, state.stop_reason, draft, executed_tools, state=state
        )
    if knowledge_state is not None and knowledge_state.has_search_evidence:
        output = AiModelOutput(
            message=_knowledge_evidence_message(system_locale, output.message, knowledge_state),
            # A safety-boundary finalization is evidence-preserving prose,
            # never an implicitly accepted code change.
            candidate=None,
        )
    elif (
        knowledge_state is not None
        and knowledge_state.stop_reason is not None
        and knowledge_state.stop_reason != _STOP_KNOWLEDGE_READY
    ):
        output = AiModelOutput(
            message=_transparent_knowledge_message(
                system_locale,
                knowledge_state.stop_reason,
                output.message,
                executed_tools,
            ),
            candidate=None,
        )
    if state.remaining_total_seconds() <= 0:
        return _fallback_assist_response(
            system_locale, _STOP_DEADLINE, draft, executed_tools, state=state
        )
    return _assist_response(output, draft, executed_tools)


def _tool_resources(
    session: Session,
    context: tools_service.ToolExecutionContext,
    *,
    reader_factory: ReaderFactory | None,
    permission_check: Callable[[], object] | None,
) -> ToolResourceFactory:
    bind = session.get_bind().engine
    return ToolResourceFactory(
        session_factory=lambda: Session(bind=bind),
        template=context,
        reader_factory=reader_factory,
        permission_check=permission_check,
    )


def _prepare_assist(
    session: Session,
    adapter_id: int,
    payload: AiAssistRequest,
    audit: tool_audit.AiToolAuditTrail,
    state: _AssistToolState,
    *,
    attachment_reader: ScopedAttachmentReader | None = None,
    reader_factory: ReaderFactory | None = None,
    permission_check: Callable[[], object] | None = None,
) -> AssistExecution:
    """Generate a candidate without writing any DLR lifecycle or version state.

    M5.7 Wave C1: when the Provider capability table explicitly supports it,
    the assist protocol additionally offers DLR's registered read-only tools
    and executes the bounded whitelist loop below. Every bound (rounds, total
    calls, per-call and accumulated result size, timeout, sequential
    execution) is a fixed constant; unknown / unregistered / write tools are
    rejected with stable error results; the loop cannot spin unboundedly; and
    the final answer still has to pass the strict AiModelOutput validation.
    """
    adapter = adapter_service.get_adapter(session, adapter_id)
    if payload.attachment_refs and attachment_reader is None:
        raise domain_error(
            503, "ai_attachment_reader_unavailable", "A scoped attachment reader is unavailable"
        )
    setting = get_setting(session)
    if setting is None:
        raise domain_error(409, "ai_not_configured", "AI model is not configured")
    draft = _setting_draft(setting)
    provider_adapter = _validate_setting(session, draft, resolve_credentials=False)
    if payload.attachment_refs and not provider_adapter.tools_supported:
        raise domain_error(
            422,
            "ai_attachment_tools_unsupported",
            "The selected model configuration does not support attachment reading tools. "
            "Choose a configuration with tool support before sending prepared attachments.",
        )
    api_key = _resolve_api_key(session, draft.credential_id)
    provider_snapshot = ProviderSnapshot.create(draft, provider_adapter, api_key)
    draft, provider_adapter, api_key = (
        provider_snapshot.draft(),
        provider_snapshot.adapter,
        provider_snapshot.api_key,
    )
    parsed_attachments: list[attachments_service.ParsedText] = []
    native_images: list[attachments_service.NativeImage] = []
    if payload.attachments:
        # M5.7 Wave B2: capability-gated processing. Images go provider-native
        # only when the capability table explicitly allows them; everything
        # else is parsed server-side into bounded text. Each attachment gets
        # an equal share of the total parsed-text budget (never more than the
        # per-file cap) so the context stays deterministic and bounded
        # regardless of file count. Decoded sizes accumulate against the total
        # byte limit in request order.
        char_budget = min(
            attachments_service.MAX_PARSED_CHARS_PER_FILE,
            attachments_service.MAX_PARSED_TOTAL_CHARS // len(payload.attachments),
        )
        total_bytes = 0
        try:
            for entry in payload.attachments:
                result = attachments_service.process_attachment(
                    entry.filename,
                    entry.content_type,
                    entry.data_base64,
                    provider_adapter.images_native,
                    char_budget,
                )
                total_bytes += result.size_bytes
                if total_bytes > attachments_service.MAX_TOTAL_BYTES:
                    raise attachments_service.AttachmentError("ai_attachment_total_too_large")
                if isinstance(result, attachments_service.NativeImage):
                    native_images.append(result)
                else:
                    parsed_attachments.append(result)
        except attachments_service.AttachmentError as error:
            _raise_attachment_error(error)
    knowledge_search_enabled = payload.knowledge_search_enabled
    knowledge_available = True
    if knowledge_search_enabled:
        available, _reason = knowledge_source_service_config.knowledge_search_capability(session)
        knowledge_available = available
    source_snapshot = (
        knowledge_service.snapshot_source(session)
        if knowledge_search_enabled and knowledge_available
        else None
    )
    tools_enabled = provider_adapter.tools_supported
    system_locale = locale_service.get_system_locale(session)
    attachment_overview = attachment_reader.overview() if attachment_reader is not None else None
    overview_items = (attachment_overview or {}).get("attachments", [])
    if not isinstance(overview_items, list):
        overview_items = []
    messages = _assist_messages(
        session,
        adapter.id,
        adapter.language,
        system_locale,
        payload,
        parsed_attachments=parsed_attachments,
        native_images=native_images,
        tools_enabled=tools_enabled,
        knowledge_search_enabled=knowledge_search_enabled and knowledge_available and tools_enabled,
        attachment_overview=attachment_overview,
        attachment_capabilities=(
            {
                "images_native": provider_adapter.images_native,
                "native_pdf": provider_adapter.native_pdf,
                "capability_source": provider_adapter.capability_source,
                "tools_available": tools_enabled,
            }
            if attachment_reader is not None
            else None
        ),
    )
    tools_payload = (
        tools_service.tools_payload(
            include_knowledge=knowledge_search_enabled,
            include_attachments=attachment_reader is not None,
        )
        if tools_enabled
        else None
    )
    # M5.7 Wave C2: per-execution tool context. The request's DB session lets
    # knowledge handlers resolve DLR Credentials inside the Secret Store;
    # ``secret_values`` collects the resolved knowledge-source credential
    # truth so every sanitization path redacts it by exact value. The ima
    # credential values are pre-resolved (best effort) so even the model's
    # own tool-call echo and early summaries are redacted.
    tool_context = tools_service.ToolExecutionContext(
        session=None,
        secret_values=list(source_snapshot.redact_values()) if source_snapshot is not None else [],
        knowledge_search_enabled=knowledge_search_enabled,
        attachment_reader=attachment_reader,
        attachment_refs=frozenset(
            item["attachment_ref"]
            for item in overview_items
            if isinstance(item, dict) and isinstance(item.get("attachment_ref"), str)
        ),
        hard_deadline=state.hard_deadline,
        attachment_deadline=state.tool_deadline,
        images_native=provider_adapter.images_native,
        native_pdf=provider_adapter.native_pdf,
        source_factory=source_snapshot.build if source_snapshot is not None else None,
    )
    tool_resources = _tool_resources(
        session,
        tool_context,
        reader_factory=reader_factory,
        permission_check=permission_check,
    )
    return AssistExecution(
        provider=provider_snapshot,
        messages=messages,
        tools=tools_payload,
        tool_resources=tool_resources,
        state=state,
        audit=audit,
        system_locale=system_locale,
        knowledge_enabled=knowledge_search_enabled,
        knowledge_available=knowledge_available,
        scoped_attachments=attachment_reader is not None,
        has_native_images=bool(native_images),
    )


def _assist_impl(
    session: Session,
    adapter_id: int,
    payload: AiAssistRequest,
    audit: tool_audit.AiToolAuditTrail,
    state: _AssistToolState,
    *,
    attachment_reader: ScopedAttachmentReader | None = None,
    reader_factory: ReaderFactory | None = None,
    permission_check: Callable[[], object] | None = None,
) -> AiAssistResponse:
    context = _prepare_assist(
        session,
        adapter_id,
        payload,
        audit,
        state,
        attachment_reader=attachment_reader,
        reader_factory=reader_factory,
        permission_check=permission_check,
    )
    draft = context.provider.draft()
    provider_adapter, api_key = context.provider.adapter, context.provider.api_key
    messages, tools_payload = context.messages, context.tools
    tools_enabled = provider_adapter.tools_supported
    tool_resources, tool_context = context.tool_resources, context.tool_resources.template
    system_locale = context.system_locale
    knowledge_search_enabled, knowledge_available = (
        context.knowledge_enabled,
        context.knowledge_available,
    )
    native_images = context.has_native_images
    audit_redact_values = context.redact_values
    executed_tools: list[AiToolCallSummary] = []
    omitted_materials = [0]
    knowledge_state = _KnowledgeRetrievalState.create(
        knowledge_search_enabled,
        knowledge_available and tools_enabled,
    )
    if knowledge_state.stop_reason is not None:
        state.stop_reason = knowledge_state.stop_reason
        audit.record_guard(round_index=0, stop_reason=state.stop_reason)
    while state.stop_reason is None:
        provider_deadline = state.tool_deadline if tools_enabled else state.hard_deadline
        provider_timeout = max(0.0, provider_deadline - time.monotonic())
        if provider_timeout <= 0:
            state.stop_reason = _STOP_DEADLINE
            audit.record_guard(round_index=state.tool_rounds, stop_reason=state.stop_reason)
            break
        budget = context_budget.prepare_call(
            draft,
            messages,
            tools_payload,
            purpose=(
                "assist_initial"
                if state.tool_rounds == 0 and not state.attachment_read_correction
                else "assist_followup"
            ),
            adapter=provider_adapter,
        )
        _audit_provider_budget(audit, state, budget, provider_deadline)
        omitted_materials[0] += budget.omitted_materials
        if not budget.fits:
            if not executed_tools and not state.attachment_read_correction:
                raise domain_error(
                    413, "ai_context_over_budget", "AI request context exceeds the model budget"
                )
            state.stop_reason = _STOP_CONTEXT_BUDGET
            audit.record_guard(round_index=state.tool_rounds, stop_reason=state.stop_reason)
            break
        provider_timeout = max(0.0, provider_deadline - time.monotonic())
        if provider_timeout <= 0:
            state.stop_reason = _STOP_DEADLINE
            audit.record_guard(round_index=state.tool_rounds, stop_reason=state.stop_reason)
            break
        try:
            final_content, tool_calls = _call_provider_with_audit(
                draft,
                api_key,
                messages,
                tools=tools_payload,
                provider_adapter=provider_adapter,
                state=state,
                audit=audit,
                timeout_seconds=provider_timeout,
            )
        except providers.AiProviderError as error:
            # A timeout that consumed the tool phase transitions into the
            # reserved tools-disabled finalization window. Fast failures on
            # the first Provider request keep the established API contract.
            if tools_enabled and error.code == "ai_timeout" and state.remaining_tool_seconds() <= 0:
                state.stop_reason = _STOP_DEADLINE
                audit.record_guard(
                    round_index=state.tool_rounds,
                    stop_reason=state.stop_reason,
                    error_code=error.code,
                )
                break
            if executed_tools or state.attachment_read_correction:
                state.stop_reason = _STOP_PROVIDER_FAILURE
                audit.record_guard(
                    round_index=state.tool_rounds,
                    stop_reason=state.stop_reason,
                    error_code=error.code,
                )
                break
            _raise_provider_error(error)
        if tool_calls is None:
            if knowledge_state.requires_tool:
                state.record_protocol_failure()
                if state.stop_reason is not None:
                    knowledge_state.phase = "stopped"
                    knowledge_state.stop_reason = _STOP_KNOWLEDGE_SEQUENCE
                    audit.record_guard(
                        round_index=state.tool_rounds,
                        stop_reason=state.stop_reason,
                        error_code=tools_service.CODE_KNOWLEDGE_SEQUENCE,
                    )
                    break
                messages.append({"role": "system", "content": knowledge_state.correction_message()})
                continue
            assert final_content is not None
            output = _parse_with_format_correction(
                final_content,
                draft=draft,
                api_key=api_key,
                messages=messages,
                provider_adapter=provider_adapter,
                state=state,
                audit=audit,
                omitted_materials=omitted_materials,
            )
            _reject_candidate_configuration_changes(output, payload)
            if state.attachment_read_required and not state.successful_attachment_reads:
                if (
                    tools_enabled
                    and not state.attachment_read_correction
                    and not state.attachment_read_attempts
                    and state.remaining_tool_seconds() > 0
                    and state.tool_rounds < tools_service.MAX_TOOL_ROUNDS
                    and state.total_tool_calls < tools_service.MAX_TOOL_CALLS_PER_ASSIST
                ):
                    # Discard ungrounded prose rather than feeding it back as
                    # evidence. This one correction shares the original context,
                    # tool and deadline budgets, with the same frozen reader.
                    state.attachment_read_correction = True
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "DLR has not recorded a successful attachment content read "
                                "in this request. Attachment directories and preparation "
                                "status are metadata only. Before completing, call "
                                "read_attachment_page or read_attachment_sheet using an "
                                "original frozen attachment_ref from this request and a "
                                "valid bounded page or range. list_attachments and other "
                                "tools do not count as a content read. Do not claim to have "
                                "read content or produce a Candidate without a successful "
                                "read. This is the only attachment-read correction."
                            ),
                        }
                    )
                    continue
                # Failed reads (including closed scopes) never earn an extra
                # correction. Preserve their real tool records in the response.
                state.stop_reason = _STOP_ATTACHMENT_UNREAD
                audit.record_guard(round_index=state.tool_rounds, stop_reason=state.stop_reason)
                break
            if knowledge_state.has_search_evidence:
                output = AiModelOutput(
                    message=_knowledge_evidence_message(
                        system_locale, output.message, knowledge_state
                    ),
                    candidate=(None if knowledge_state.degraded_error_codes else output.candidate),
                    request_intent=output.request_intent,
                )
            response = _with_omission_note(
                _assist_response(output, draft, executed_tools), omitted_materials[0], system_locale
            )
            if state.remaining_total_seconds() <= 0:
                _raise_provider_error(providers.AiProviderError("ai_timeout"))
            return response
        if not tools_enabled:
            # Defensive: a provider without tool capability fabricated tool
            # calls; fail with the stable actionable error instead of guessing.
            for call in tool_calls:
                audit.record_tool_attempt(
                    round_index=state.tool_rounds + 1,
                    tool_name=call.name,
                    raw_arguments=call.arguments,
                    validated_arguments=tools_service.validated_tool_arguments(
                        call.name, call.arguments
                    ),
                    status="blocked",
                    duration_ms=0,
                    result_size=0,
                    result_truncated=False,
                    error_code="ai_tool_unsupported",
                    stop_reason="ai_tool_unsupported",
                    redact_values=audit_redact_values,
                )
            _raise_tool_error("ai_tool_unsupported")
        if not tool_calls:
            # A normalized empty list has no actionable call and is not a
            # valid final answer.
            _raise_provider_error(providers.AiProviderError("ai_response_invalid"))
        if not state.begin_round(len(tool_calls)):
            assert state.stop_reason is not None
            attempted_round = state.tool_rounds + 1
            for call in tool_calls:
                audit.record_tool_attempt(
                    round_index=attempted_round,
                    tool_name=call.name,
                    raw_arguments=call.arguments,
                    validated_arguments=tools_service.validated_tool_arguments(
                        call.name, call.arguments
                    ),
                    status="blocked",
                    duration_ms=0,
                    result_size=0,
                    result_truncated=False,
                    stop_reason=state.stop_reason,
                    redact_values=audit_redact_values,
                )
            break
        processed_calls: list[tuple[providers.NormalizedToolCall, tools_service.ToolExecution]] = []
        knowledge_correction_needed = False
        knowledge_progress_needed = False
        for call in tool_calls:
            original_validated_args = tools_service.validated_tool_arguments(
                call.name, call.arguments
            )
            validated_args = knowledge_state.normalize_call_arguments(
                call.name, original_validated_args
            )
            effective_arguments = call.arguments
            if validated_args != original_validated_args and validated_args is not None:
                effective_arguments = json.dumps(
                    validated_args,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            # A scoped attachment reader is an independent evidence channel.
            # It must still pass its registered dispatcher/ref/budget checks,
            # but neither advances nor fails the knowledge retrieval stages.
            scoped_attachment_call = attachment_reader is not None and call.name in (
                "list_attachments",
                "read_attachment_page",
                "read_attachment_sheet",
            )
            if not scoped_attachment_call and not knowledge_state.accepts_call(
                call.name, validated_args
            ):
                execution = tools_service.rejected_tool_call(
                    call.name, tools_service.CODE_KNOWLEDGE_SEQUENCE
                )
                executed_tools.append(_tool_call_summary(execution))
                processed_calls.append((call, execution))
                state.record_protocol_failure()
                knowledge_correction_needed = True
                audit.record_tool_attempt(
                    round_index=state.tool_rounds,
                    tool_name=call.name,
                    raw_arguments=call.arguments,
                    validated_arguments=validated_args,
                    status="blocked",
                    duration_ms=execution.duration_ms,
                    result_size=execution.result_size,
                    result_truncated=execution.result_truncated,
                    error_code=execution.error_code,
                    stop_reason=state.stop_reason,
                    redact_values=audit_redact_values,
                )
                if state.stop_reason is not None:
                    knowledge_state.phase = "stopped"
                    knowledge_state.stop_reason = _STOP_KNOWLEDGE_SEQUENCE
                    break
                continue
            # A sequence-rejected call did not reach the dispatcher. Register
            # its fingerprint only after the stage accepts it, so a corrected
            # later retry can execute once without weakening duplicate checks
            # for any call that actually reached the dispatcher.
            fingerprint = tools_service.tool_call_fingerprint(call.name, effective_arguments)
            if not state.register_fingerprint(fingerprint):
                execution = tools_service.rejected_tool_call(
                    call.name, tools_service.CODE_DUPLICATE
                )
                audit.record_tool_attempt(
                    round_index=state.tool_rounds,
                    tool_name=call.name,
                    raw_arguments=call.arguments,
                    validated_arguments=validated_args,
                    status="blocked",
                    duration_ms=execution.duration_ms,
                    result_size=execution.result_size,
                    result_truncated=execution.result_truncated,
                    error_code=execution.error_code,
                    stop_reason=state.stop_reason,
                    redact_values=audit_redact_values,
                )
                executed_tools.append(_tool_call_summary(execution))
                processed_calls.append((call, execution))
                break
            with tool_resources.open() as execution_context:
                execution = tools_service.execute_tool_call(
                    call.name, effective_arguments, api_key, context=execution_context
                )
            state.record_execution(execution)
            if not scoped_attachment_call:
                knowledge_state.record_execution(validated_args, execution)
                knowledge_correction_needed = False
            if call.name in ("list_knowledge_bases", "search_knowledge", "read_knowledge"):
                knowledge_progress_needed = True
            if knowledge_state.stop_reason is not None and state.stop_reason is None:
                state.stop_reason = knowledge_state.stop_reason
            if state.stop_reason is None and state.remaining_tool_seconds() <= 0:
                state.stop_reason = _STOP_DEADLINE
            audit.record_tool_attempt(
                round_index=state.tool_rounds,
                tool_name=call.name,
                raw_arguments=call.arguments,
                validated_arguments=validated_args,
                status=execution.status,  # type: ignore[arg-type]
                duration_ms=execution.duration_ms,
                result_size=execution.result_size,
                result_truncated=execution.result_truncated,
                error_code=execution.error_code,
                stop_reason=state.stop_reason,
                redact_values=audit_redact_values,
            )
            executed_tools.append(_tool_call_summary(execution))
            processed_calls.append((call, execution))
            if state.stop_reason is not None:
                break
        if processed_calls:
            assistant_content = (
                final_content if final_content is not None and final_content.strip() else None
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant_content,
                    "tool_calls": [
                        _assistant_tool_call(call, api_key, tool_context)
                        for call, _execution in processed_calls
                    ],
                }
            )
            if tool_calls[0].anthropic_blocks and len(processed_calls) == len(tool_calls):
                private_blocks = list(tool_calls[0].anthropic_blocks)
                # Signed turns must be echoed verbatim. If existing safety redaction
                # would change a tool argument, reject rather than corrupt the turn.
                for call, _execution in processed_calls:
                    safe_call = _assistant_tool_call(call, api_key, tool_context)
                    if providers.load_json_strict(
                        cast(dict[str, str], safe_call["function"])["arguments"]
                    ) != (providers.load_json_strict(call.arguments)):
                        raise AiResponseInvalid("output_safety", "secret_reflection")
                messages[-1]["_anthropic_continuation"] = private_blocks
            tool_messages: list[providers.JsonObject] = []
            supplementary: list[providers.JsonObject] = []
            for call, execution in processed_calls:
                result_message, visual_message = providers.tool_result_messages(
                    call.id,
                    call.name,
                    tools_service.tool_result_content(execution),
                    execution.media,
                    provider_adapter,
                )
                tool_messages.append(result_message)
                if visual_message is not None:
                    supplementary.append(visual_message)
            messages.extend(tool_messages)
            messages.extend(supplementary)
        if knowledge_correction_needed and state.stop_reason is None:
            messages.append({"role": "system", "content": knowledge_state.correction_message()})
        elif knowledge_progress_needed and state.stop_reason is None:
            messages.append({"role": "system", "content": knowledge_state.progress_message()})
        if state.stop_reason is not None:
            break
        if state.tool_rounds >= tools_service.MAX_TOOL_ROUNDS:
            state.stop_reason = _STOP_ROUND_BUDGET
            audit.record_guard(round_index=state.tool_rounds, stop_reason=state.stop_reason)
        elif state.remaining_tool_seconds() <= 0:
            state.stop_reason = _STOP_DEADLINE
            audit.record_guard(round_index=state.tool_rounds, stop_reason=state.stop_reason)

    state.prepared_knowledge_incomplete = (
        state.attachment_read_required
        and knowledge_search_enabled
        and (
            knowledge_state.requires_tool
            or knowledge_state.needs_cross_base_search
            or not knowledge_state.has_search_evidence
        )
    )
    if knowledge_state.requires_tool:
        knowledge_state.phase = "stopped"
        knowledge_state.stop_reason = (
            _STOP_KNOWLEDGE_SEQUENCE
            if knowledge_state.has_search_evidence
            else (
                _STOP_KNOWLEDGE_SEARCH_EMPTY
                if knowledge_state.search_attempts
                else _STOP_KNOWLEDGE_SEQUENCE
            )
        )
    response = _finalize_after_tool_stop(
        state=state,
        system_locale=system_locale,
        draft=draft,
        api_key=api_key,
        messages=messages,
        image_input=bool(native_images),
        provider_adapter=provider_adapter,
        payload=payload,
        executed_tools=executed_tools,
        knowledge_state=knowledge_state,
        omitted_materials=omitted_materials,
        audit=audit,
    )
    return _with_omission_note(response, omitted_materials[0], system_locale)


def assist(
    session: Session,
    adapter_id: int,
    payload: AiAssistRequest,
    *,
    hard_deadline: float | None = None,
    attachment_reader: ScopedAttachmentReader | None = None,
    reader_factory: ReaderFactory | None = None,
    permission_check: Callable[[], object] | None = None,
) -> AiAssistResponse:
    """Run one request-correlated Assist and always persist its terminal state."""

    audit = tool_audit.AiToolAuditTrail(
        correlation=tool_audit.new_request_correlation(payload.conversation_id),
        adapter_id=adapter_id,
    )
    now = time.monotonic()
    state = _AssistToolState.create(
        settings.ai_assist_total_timeout_seconds
        if hard_deadline is None
        else max(0.0, hard_deadline - now),
        now=now,
        correlation=audit.correlation,
    )
    state.attachment_read_required = bool(payload.attachment_refs)
    try:
        prepared = PreparedAssist.create(adapter_id, payload)

        def execute_legacy(request: PreparedAssist) -> AiAssistResponse:
            return _assist_impl(
                session,
                request.adapter_id,
                request.request(),
                audit,
                state,
                attachment_reader=attachment_reader,
                reader_factory=reader_factory,
                permission_check=permission_check,
            )

        if settings.ai_assist_runtime == "legacy":
            outcome = LegacyRuntime().execute(
                prepared, RuntimeResources(legacy_execute=execute_legacy)
            )
        else:
            from dlr.control.ai.runtime.pydantic_ai.runtime import PydanticAiRuntime

            context = _prepare_assist(
                session,
                adapter_id,
                prepared.request(),
                audit,
                state,
                attachment_reader=attachment_reader,
                reader_factory=reader_factory,
                permission_check=permission_check,
            )
            try:
                outcome = PydanticAiRuntime().execute(
                    prepared, RuntimeResources(legacy_execute=execute_legacy, execution=context)
                )
            except providers.AiProviderError as error:
                _raise_provider_error(error)
        response = outcome.response()
        if permission_check is not None:
            permission_check()
        if state.remaining_total_seconds() <= 0 and not (
            state.server_fallback_returned and response.candidate is None
        ):
            _raise_provider_error(providers.AiProviderError("ai_timeout"))
    except BaseException as error:
        if isinstance(error, AiResponseInvalid):
            audit.record_response_failure(error.stage, error.reason)
        audit.finish(status="error", error_code=_audit_error_code(error))
        raise
    response = complete_response(payload, response, state)
    audit.finish(status="stopped" if audit.stop_reason is not None else "success")
    return response
