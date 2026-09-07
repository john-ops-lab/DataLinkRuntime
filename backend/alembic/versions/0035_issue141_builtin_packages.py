"""Persistent offline dependency library and admission snapshots."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035_builtin_packages"
down_revision: str | None = "0034_webhook_response"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "builtin_package_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("quota_bytes", sa.BigInteger(), nullable=False, server_default="1073741824"),
        sa.CheckConstraint("id = 1 AND quota_bytes > 0", name="ck_builtin_settings"),
    )
    op.execute("INSERT INTO builtin_package_settings (id) VALUES (1)")
    op.create_table(
        "builtin_packages",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("environment", sa.String(512), nullable=False),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column("repository_path", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(36), nullable=False, unique=True),
        sa.Column("package_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="uploaded"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("kind", "name", "version", "environment", name="uq_builtin_identity"),
        sa.UniqueConstraint("kind", "repository_path", name="uq_builtin_path"),
        sa.CheckConstraint(
            "kind IN ('pypi', 'npm', 'maven') AND size_bytes > 0", name="ck_builtin_package"
        ),
        sa.CheckConstraint("status IN ('uploaded', 'deleting')", name="ck_builtin_status"),
    )
    op.create_table(
        "builtin_package_uploads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column("repository_path", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "kind IN ('pypi', 'npm', 'maven') AND size_bytes > 0", name="ck_builtin_upload"
        ),
    )
    op.add_column(
        "executions", sa.Column("builtin_package_snapshot", postgresql.JSONB(), nullable=True)
    )
    op.add_column(
        "executions",
        sa.Column("dependency_check", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Keep existing default selection and credentials untouched.
    connection = op.get_bind()
    for kind in ("pypi", "npm", "maven"):
        base = f"DLR builtin source ({kind})"
        name, suffix = base, 1
        while connection.scalar(
            sa.text("SELECT 1 FROM package_sources WHERE name = :name").bindparams(name=name)
        ):
            name = f"{base} {suffix}"
            suffix += 1
        op.execute(
            sa.text(
                "INSERT INTO package_sources (name, kind, index_url, is_default) "
                "VALUES (:name, :kind, :url, false)"
            ).bindparams(name=name, kind=kind, url=f"dlr-builtin://{kind}")
        )


def downgrade() -> None:
    op.execute("DELETE FROM package_sources WHERE index_url LIKE 'dlr-builtin://%'")
    op.drop_column("executions", "dependency_check")
    op.drop_column("executions", "builtin_package_snapshot")
    op.drop_table("builtin_package_uploads")
    op.drop_table("builtin_packages")
    op.drop_table("builtin_package_settings")
