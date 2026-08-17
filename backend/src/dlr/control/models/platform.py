"""Platform configuration entities: secrets, dependency sources and AI settings.

Secret Store contract:

- ``Credential.ciphertext`` holds only Fernet ciphertext; plaintext secret
  values are never persisted and never returned by the API after creation.
- ``AdapterCredentialBinding`` maps one Adapter environment key
  (``context.secrets.get(env_key)``) to one field of one credential.
- ``PackageSource`` is the platform-managed PyPI, npm or Maven configuration
  used by Workers when preparing version dependencies.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from dlr.control.db import Base

# Fields each credential type exposes to bindings.
CREDENTIAL_FIELDS: dict[str, tuple[str, ...]] = {
    "password": ("username", "password"),
    "token": ("token",),
    "access_key": ("access_key_id", "access_key_secret"),
    "secret": ("value",),
}

# M5.5.7: field names of the ``access_key`` type were standardized to
# ``access_key_id`` / ``access_key_secret``. The type name stays
# ``access_key``; this map only carries legacy field spellings accepted on
# read (credentials encrypted before the rename), so existing bindings keep
# working without exposing any plaintext.
LEGACY_ACCESS_KEY_FIELDS: dict[str, str] = {
    "access_key": "access_key_id",
    "secret_key": "access_key_secret",
}


class Credential(Base):
    """One named business secret (encrypted at rest with the Master Key)."""

    __tablename__ = "credentials"
    __table_args__ = (
        CheckConstraint(
            "type IN ('password', 'token', 'access_key', 'secret')",
            name="ck_credentials_type",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AdapterCredentialBinding(Base):
    """Maps ``env_key`` on one Adapter to one field of one Credential."""

    __tablename__ = "adapter_credential_bindings"
    __table_args__ = (
        UniqueConstraint(
            "adapter_id", "env_key", name="uq_adapter_credential_bindings_adapter_env_key"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    adapter_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("adapters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    env_key: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("credentials.id", ondelete="RESTRICT"),
        nullable=False,
    )
    field: Mapped[str] = mapped_column(String(64), nullable=False)


class PackageSource(Base):
    """One platform-managed PyPI, npm or Maven dependency source."""

    __tablename__ = "package_sources"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('pypi', 'npm', 'maven')",
            name="ck_package_sources_kind",
        ),
        CheckConstraint(
            "preset_id IS NULL OR preset_id IN ("
            "'pypi.aliyun', 'pypi.official', 'npm.npmmirror', 'npm.official', "
            "'maven.aliyun', 'maven.central')",
            name="ck_package_sources_preset_id",
        ),
        # At most one default source for each kind.
        Index(
            "uq_package_sources_default",
            "kind",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pypi", server_default=text("'pypi'")
    )
    index_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Stable identity for a genuine platform preset; user-created sources are
    # NULL even when their URL happens to equal a preset URL.
    preset_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Optional credential reference for authenticated indexes.
    credential_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("credentials.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AiModelSetting(Base):
    """The single global active AI provider configuration (M4).

    API key plaintext never lives here. ``credential_id`` may only reference
    a token Credential and is resolved in memory immediately before a provider
    call.
    """

    __tablename__ = "ai_model_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_ai_model_settings_singleton"),
        CheckConstraint(
            "provider IN ('openai', 'deepseek', 'kimi', 'minimax', 'custom_openai_compatible')",
            name="ck_ai_model_settings_provider",
        ),
        CheckConstraint(
            "reasoning_mode IN ('default', 'enabled', 'disabled')",
            name="ck_ai_model_settings_reasoning_mode",
        ),
        CheckConstraint(
            "reasoning_effort IS NULL OR "
            "reasoning_effort IN ('low', 'medium', 'high', 'max', 'xhigh')",
            name="ck_ai_model_settings_reasoning_effort",
        ),
    )

    # A fixed primary key plus a database check makes the singleton invariant
    # explicit and race-safe when the service performs an ON CONFLICT upsert.
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    credential_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("credentials.id", ondelete="SET NULL"),
        nullable=True,
    )
    reasoning_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="default", server_default=text("'default'")
    )
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
