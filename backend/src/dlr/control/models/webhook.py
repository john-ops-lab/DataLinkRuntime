"""Adapter Webhook persistence model (M5.3).

Contracts kept by this model:

- At most one Webhook per Adapter (``adapter_id`` is unique); no generic
  Trigger table and no Webhook request history table — accepted requests
  are carried by Execution history, rejected ones are never persisted.
- ``public_id`` is a random, unpredictable identifier used only for
  routing; it is never an authentication secret and never exposes the
  Adapter id. Authentication is the referenced token Credential.
- The Webhook row is deployment configuration of the Adapter: it is
  removed with the Adapter (ON DELETE CASCADE) and never participates in
  the execution-history delete protection.
- ``credential_id`` references a token-type Credential with RESTRICT: a
  Credential still used by a Webhook can never be deleted, so the
  external ingress can never silently become unauthenticated.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base


class AdapterWebhook(Base):
    """The single Webhook configuration of one Adapter (singleton per Adapter)."""

    __tablename__ = "adapter_webhooks"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    adapter_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("adapters.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # Random token-safe identifier; routing only, never an auth secret.
    # Stable after creation: PUT never rotates it (no URL rotation in M5.3).
    public_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Must reference a token-type Credential; RESTRICT blocks deleting a
    # Credential that a Webhook still uses.
    credential_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("credentials.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
