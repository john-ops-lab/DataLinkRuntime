"""Pydantic schemas for the Adapter Webhook final user model (M5.4.3)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

WEBHOOK_PUBLIC_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{2,63}$"


class WebhookUpsert(BaseModel):
    """Request body for PUT /api/adapters/{adapter_id}/webhook.

    The row exists from Adapter creation. PUT replaces the editable path,
    optional token Credential reference and receiving state. Enabling adds
    runtime readiness and enabled-path uniqueness gates.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    # The service enforces WEBHOOK_PUBLIC_ID_PATTERN whenever the path is
    # changed. Unchanged M5.3 token_urlsafe paths remain accepted so an
    # upgraded running Webhook can still Stop/Start without changing its URL.
    public_id: str = Field(min_length=3, max_length=64)
    credential_id: int | None


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
    credential_id: int | None
    credential_name: str | None
    created_at: datetime
    updated_at: datetime
