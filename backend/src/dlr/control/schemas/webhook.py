"""Pydantic schemas for the Adapter Webhook configuration API (M5.3)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class WebhookUpsert(BaseModel):
    """Request body for PUT /api/adapters/{adapter_id}/webhook.

    PUT is create-or-update: ``enabled`` and ``credential_id`` are both
    mandatory and the saved Webhook becomes exactly the submitted
    configuration. The referenced Credential must be of type ``token``.
    The ``public_id`` is server-generated on first creation and stable.
    """

    enabled: bool
    credential_id: int


class WebhookResponse(BaseModel):
    """Current Webhook configuration of one Adapter.

    Never carries Credential plaintext or ciphertext: only the Credential
    name for display. ``hook_path`` is the external entry path built from
    the routing-only ``public_id``.
    """

    model_config = ConfigDict(from_attributes=True)

    adapter_id: int
    enabled: bool
    public_id: str
    hook_path: str
    credential_id: int
    credential_name: str
    created_at: datetime
    updated_at: datetime
