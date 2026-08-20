"""Persistence and bounded validation for productized KnowledgeSources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.ai.knowledge import (
    KS_AUTH_FAILED,
    KS_CONFIG_INVALID,
    KS_CREDENTIAL_INVALID,
    KS_DNS_FAILED,
    KS_NOT_CONFIGURED,
    KS_RATE_LIMITED,
    KS_RESPONSE_INVALID,
    KS_TIMEOUT,
    KS_TOO_LARGE,
    KS_UNREACHABLE,
    KS_UNSUPPORTED,
    KS_UPSTREAM_ERROR,
    KnowledgeBaseSummary,
    KnowledgeSourceError,
)
from dlr.control.models import Credential, KnowledgeSourceSetting
from dlr.control.schemas.knowledge_source import (
    KnowledgeBaseResponse,
    KnowledgeSourceConfigStatus,
    KnowledgeSourceId,
    KnowledgeSourceResponse,
    KnowledgeSourceTestResponse,
    KnowledgeSourceTestStatus,
    KnowledgeSourceUpdate,
)
from dlr.control.services.adapter import domain_error

IMA_SOURCE_ID: KnowledgeSourceId = "ima"
IMA_DISPLAY_NAME = "Tencent ima"
DEFAULT_IMA_ENDPOINT = "https://ima.qq.com"
_SINGLETON_ID = 1


@dataclass(frozen=True)
class ImaEffectiveConfig:
    """The effective non-secret ima configuration for one request."""

    enabled: bool
    credential_id: int | None
    credential_name: str | None
    endpoint: str
    allowed_hosts: str
    allow_http: bool
    timeout_seconds: float
    config_source: Literal["database", "environment"]
    created_at: datetime | None
    updated_at: datetime | None


def _ensure_ima_source(source_id: str) -> None:
    if source_id != IMA_SOURCE_ID:
        raise domain_error(404, "knowledge_source_not_found", "Knowledge source not found")


def get_setting(session: Session) -> KnowledgeSourceSetting | None:
    """Return the product configuration row, if the administrator saved one."""
    return session.get(KnowledgeSourceSetting, _SINGLETON_ID)


def _credential_by_name(session: Session, name: str | None) -> Credential | None:
    if not name:
        return None
    return session.scalar(select(Credential).where(Credential.name == name))


def _database_endpoint() -> str:
    """Use a deployment override only when it is explicitly non-default.

    The UI/API never accepts an endpoint.  ``DLR_IMA_ENDPOINT`` remains the
    advanced deployment-only override, while an empty legacy Compose value
    cannot accidentally disable a saved product configuration.
    """
    endpoint = settings.dlr_ima_endpoint.strip()
    return endpoint if endpoint and endpoint != DEFAULT_IMA_ENDPOINT else DEFAULT_IMA_ENDPOINT


def _display_endpoint(endpoint: str) -> str:
    """Keep malformed credential-bearing deployment URLs out of metadata."""
    try:
        parsed = urlsplit(endpoint)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return "[REDACTED]"
    except ValueError:
        return "[REDACTED]"
    return endpoint or DEFAULT_IMA_ENDPOINT


def effective_ima_config(session: Session | None) -> ImaEffectiveConfig:
    """Resolve DB configuration first, then the legacy environment fallback."""
    setting = get_setting(session) if session is not None else None
    if setting is not None:
        credential_name = None
        if setting.credential_id is not None and session is not None:
            credential = session.get(Credential, setting.credential_id)
            credential_name = credential.name if credential is not None else None
        return ImaEffectiveConfig(
            enabled=setting.enabled,
            credential_id=setting.credential_id,
            credential_name=credential_name,
            endpoint=_database_endpoint(),
            allowed_hosts=settings.dlr_ima_allowed_hosts,
            allow_http=settings.dlr_ima_allow_http,
            timeout_seconds=settings.dlr_ima_timeout_seconds,
            config_source="database",
            created_at=setting.created_at,
            updated_at=setting.updated_at,
        )

    endpoint = settings.dlr_ima_endpoint.strip()
    credential_name = (
        settings.dlr_ima_credential_name.strip() if settings.dlr_ima_credential_name else None
    )
    credential_id = None
    if session is not None:
        credential = _credential_by_name(session, credential_name)
        credential_id = credential.id if credential is not None else None
    return ImaEffectiveConfig(
        enabled=bool(endpoint),
        credential_id=credential_id,
        credential_name=credential_name,
        endpoint=endpoint,
        allowed_hosts=settings.dlr_ima_allowed_hosts,
        allow_http=settings.dlr_ima_allow_http,
        timeout_seconds=settings.dlr_ima_timeout_seconds,
        config_source="environment",
        created_at=None,
        updated_at=None,
    )


def _effective_credential(session: Session, config: ImaEffectiveConfig) -> Credential | None:
    if config.credential_id is not None:
        return session.get(Credential, config.credential_id)
    return _credential_by_name(session, config.credential_name)


def config_status(session: Session, config: ImaEffectiveConfig) -> KnowledgeSourceConfigStatus:
    """Classify saved/fallback metadata without decrypting Secret values."""
    if not config.enabled:
        return "disabled"
    credential = _effective_credential(session, config)
    if credential is None or credential.type != "access_key":
        return "unconfigured"
    return "configured"


def setting_response(session: Session, source_id: str = IMA_SOURCE_ID) -> KnowledgeSourceResponse:
    """Build the metadata-only configuration response."""
    _ensure_ima_source(source_id)
    config = effective_ima_config(session)
    credential = _effective_credential(session, config)
    return KnowledgeSourceResponse(
        source_id=IMA_SOURCE_ID,
        kind=IMA_SOURCE_ID,
        name=IMA_DISPLAY_NAME,
        endpoint=_display_endpoint(config.endpoint),
        enabled=config.enabled,
        status=config_status(session, config),
        credential_id=config.credential_id,
        credential_name=credential.name if credential is not None else config.credential_name,
        credential_type=credential.type if credential is not None else None,
        config_source=config.config_source,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


def list_settings(session: Session) -> list[KnowledgeSourceResponse]:
    """List the fixed first provider without opening a generic registry."""
    return [setting_response(session)]


def _validate_credential(session: Session, credential_id: int | None) -> Credential | None:
    if credential_id is None:
        return None
    credential = session.get(Credential, credential_id)
    if credential is None or credential.type != "access_key":
        raise domain_error(
            422,
            "knowledge_source_credential_invalid",
            "Knowledge source requires an access_key Credential",
        )
    return credential


def save_setting(
    session: Session,
    data: KnowledgeSourceUpdate,
    source_id: str = IMA_SOURCE_ID,
) -> KnowledgeSourceResponse:
    """Atomically persist the singleton ima configuration."""
    _ensure_ima_source(source_id)
    _validate_credential(session, data.credential_id)
    statement = insert(KnowledgeSourceSetting).values(
        id=_SINGLETON_ID,
        source_id=IMA_SOURCE_ID,
        enabled=data.enabled,
        credential_id=data.credential_id,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[KnowledgeSourceSetting.id],
        set_={
            "source_id": statement.excluded.source_id,
            "enabled": statement.excluded.enabled,
            "credential_id": statement.excluded.credential_id,
            "updated_at": statement.excluded.created_at,
        },
    )
    session.execute(statement)
    session.commit()
    if get_setting(session) is None:  # defensive: the upsert guarantees this row
        raise RuntimeError("KnowledgeSource setting upsert did not create the singleton row")
    return setting_response(session)


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    sanitized = value
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized


def _knowledge_bases(
    source: object, items: list[KnowledgeBaseSummary]
) -> list[KnowledgeBaseResponse]:
    redact_values = tuple(
        value for value in getattr(source, "redact_values", lambda: ())() if isinstance(value, str)
    )
    return [
        KnowledgeBaseResponse(
            id=_redact_text(item.id, redact_values),
            name=_redact_text(item.name, redact_values),
            status="accessible",
        )
        for item in items
    ]


def _test_status(config: ImaEffectiveConfig, code: str) -> KnowledgeSourceTestStatus:
    if code == KS_NOT_CONFIGURED:
        return "disabled" if not config.enabled else "unconfigured"
    return "error"


_TEST_ERROR_MESSAGES = {
    KS_NOT_CONFIGURED: "Knowledge source is not configured",
    KS_CREDENTIAL_INVALID: "Knowledge source Credential is invalid",
    KS_CONFIG_INVALID: "Knowledge source configuration is invalid",
    KS_AUTH_FAILED: "Knowledge source authentication failed",
    KS_DNS_FAILED: "Knowledge source hostname could not be resolved",
    KS_UNREACHABLE: "Knowledge source is unreachable",
    KS_RATE_LIMITED: "Knowledge source rate limit was reached",
    KS_TIMEOUT: "Knowledge source request timed out",
    KS_RESPONSE_INVALID: "Knowledge source returned an invalid response",
    KS_TOO_LARGE: "Knowledge source response is too large",
    KS_UNSUPPORTED: "Knowledge source operation is unsupported",
    KS_UPSTREAM_ERROR: "Knowledge source returned an upstream error",
}


def test_connection(
    session: Session, source_id: str = IMA_SOURCE_ID
) -> KnowledgeSourceTestResponse:
    """Run the bounded read-only list operation as a connection validation."""
    _ensure_ima_source(source_id)
    config = effective_ima_config(session)
    if not config.enabled or not config.endpoint:
        return KnowledgeSourceTestResponse(
            ok=False,
            status="disabled",
            error_code=KS_NOT_CONFIGURED,
            message=_TEST_ERROR_MESSAGES[KS_NOT_CONFIGURED],
            knowledge_bases=[],
        )
    try:
        from dlr.control.ai import ima

        source = ima.build_source(session)
        items = source.list_knowledge_bases()
        return KnowledgeSourceTestResponse(
            ok=True,
            status="connected",
            message="Knowledge source connection validated",
            knowledge_bases=_knowledge_bases(source, items),
        )
    except KnowledgeSourceError as error:
        return KnowledgeSourceTestResponse(
            ok=False,
            status=_test_status(config, error.code),
            error_code=error.code,
            message=_TEST_ERROR_MESSAGES.get(error.code, "Knowledge source request failed"),
            knowledge_bases=[],
        )


def list_knowledge_bases(
    session: Session, source_id: str = IMA_SOURCE_ID
) -> list[KnowledgeBaseResponse]:
    """List accessible KnowledgeSource metadata for the Settings UI."""
    _ensure_ima_source(source_id)
    try:
        from dlr.control.ai import ima

        source = ima.build_source(session)
        return _knowledge_bases(source, source.list_knowledge_bases())
    except KnowledgeSourceError as error:
        status_code = 422 if error.code in {KS_CONFIG_INVALID, KS_CREDENTIAL_INVALID} else 502
        raise domain_error(status_code, error.code, "Knowledge source request failed") from None
