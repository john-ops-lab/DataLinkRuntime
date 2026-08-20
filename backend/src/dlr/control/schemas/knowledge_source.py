"""Typed administrator API contracts for productized KnowledgeSources."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

KnowledgeSourceId = Literal["ima"]
KnowledgeSourceConfigStatus = Literal["disabled", "unconfigured", "configured"]
KnowledgeSourceTestStatus = Literal["disabled", "unconfigured", "connected", "error"]
KnowledgeBaseStatus = Literal["accessible"]


class KnowledgeSourceUpdate(BaseModel):
    """Complete replacement for the first ima source configuration.

    Endpoint is deliberately absent.  The product uses the official ima
    endpoint; deployment-level ``DLR_IMA_ENDPOINT`` remains the only custom
    endpoint override and is never edited by this API.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    credential_id: int | None = None


class KnowledgeBaseResponse(BaseModel):
    """Safe, bounded metadata for one accessible knowledge base."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    status: KnowledgeBaseStatus


class KnowledgeSourceResponse(BaseModel):
    """Configuration metadata; no Credential ciphertext or Secret values."""

    model_config = ConfigDict(extra="forbid")

    source_id: KnowledgeSourceId
    kind: KnowledgeSourceId
    name: str
    endpoint: str
    enabled: bool
    status: KnowledgeSourceConfigStatus
    credential_id: int | None
    credential_name: str | None
    credential_type: str | None
    config_source: Literal["database", "environment"]
    created_at: datetime | None
    updated_at: datetime | None


class KnowledgeSourceTestResponse(BaseModel):
    """Result of a bounded read-only connection/list validation."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    status: KnowledgeSourceTestStatus
    error_code: str | None = None
    message: str
    knowledge_bases: list[KnowledgeBaseResponse]
