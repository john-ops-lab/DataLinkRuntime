"""Read-only current configuration references for the admin credential catalog.

This is not an assertion that immutable executions no longer use a credential.
No credential fields are decrypted and no connection URLs are returned.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from dlr.control.models import Adapter, AdapterWebhook
from dlr.control.models.knowledge_source import KnowledgeSourceSetting
from dlr.control.models.platform import (
    AdapterCredentialBinding,
    AiCustomProvider,
    AiModelSetting,
    PackageSource,
)
from dlr.control.schemas.credential import CredentialUsageItem, CredentialUsageResponse
from dlr.control.services import secrets


def get_usage(session: Session, credential_id: int) -> CredentialUsageResponse:
    secrets.get_credential(session, credential_id)
    items: list[CredentialUsageItem] = []
    bindings = session.execute(
        select(Adapter.id, Adapter.name, AdapterCredentialBinding.env_key)
        .join(AdapterCredentialBinding, AdapterCredentialBinding.adapter_id == Adapter.id)
        .where(
            Adapter.archived_at.is_(None), AdapterCredentialBinding.credential_id == credential_id
        )
        .order_by(Adapter.id, AdapterCredentialBinding.env_key)
    )
    for adapter_id, name, key in bindings:
        items.append(
            CredentialUsageItem(
                kind="adapter_binding", name=name, adapter_id=adapter_id, detail=key
            )
        )
    webhooks = session.execute(
        select(Adapter.id, Adapter.name)
        .join(AdapterWebhook, AdapterWebhook.adapter_id == Adapter.id)
        .where(Adapter.archived_at.is_(None), AdapterWebhook.credential_id == credential_id)
        .order_by(Adapter.id)
    )
    for adapter_id, name in webhooks:
        items.append(CredentialUsageItem(kind="webhook", name=name, adapter_id=adapter_id))
    for name in session.scalars(
        select(AiModelSetting.model).where(AiModelSetting.credential_id == credential_id)
    ):
        items.append(CredentialUsageItem(kind="ai_model", name=name))
    for name in session.scalars(
        select(AiCustomProvider.name)
        .where(AiCustomProvider.credential_id == credential_id)
        .order_by(AiCustomProvider.id)
    ):
        items.append(CredentialUsageItem(kind="ai_custom_provider", name=name))
    for name in session.scalars(
        select(KnowledgeSourceSetting.source_id).where(
            KnowledgeSourceSetting.credential_id == credential_id
        )
    ):
        items.append(CredentialUsageItem(kind="knowledge_source", name=name))
    for name in session.scalars(
        select(PackageSource.name)
        .where(PackageSource.credential_id == credential_id)
        .order_by(PackageSource.id)
    ):
        items.append(CredentialUsageItem(kind="package_source", name=name))
    return CredentialUsageResponse(items=items)
