"""M5.5.7 access_key field standardization.

The ``access_key`` Credential type now exposes ``access_key_id`` +
``access_key_secret``. Binding rows that still reference the legacy field
spellings (``access_key`` / ``secret_key``) are rewritten to the new names
so existing Adapters keep resolving. Ciphertext is Fernet-encrypted with the
deployment Master Key and cannot be touched here; the secrets service maps
legacy keys transparently on read instead.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_m5_5_7_access_key_fields"
down_revision: str | None = "0012_m5_5_9_active_name_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE adapter_credential_bindings "
        "SET field = 'access_key_id' WHERE field = 'access_key'"
    )
    op.execute(
        "UPDATE adapter_credential_bindings "
        "SET field = 'access_key_secret' WHERE field = 'secret_key'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE adapter_credential_bindings "
        "SET field = 'access_key' WHERE field = 'access_key_id'"
    )
    op.execute(
        "UPDATE adapter_credential_bindings "
        "SET field = 'secret_key' WHERE field = 'access_key_secret'"
    )
