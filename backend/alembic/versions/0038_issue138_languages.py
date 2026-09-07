"""Add TypeScript / Go adapter languages and Go module sources."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_issue138_languages"
down_revision: str | None = "0037_portable_simplified"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _constraints(*, expanded: bool) -> None:
    languages = "'python', 'javascript', 'java'" + (", 'typescript', 'go'" if expanded else "")
    kinds = "'pypi', 'npm', 'maven'" + (", 'goproxy'" if expanded else "")
    presets = (
        "'pypi.aliyun', 'pypi.official', 'npm.npmmirror', 'npm.official', "
        "'maven.aliyun', 'maven.central'"
    )
    if expanded:
        presets += ", 'goproxy.cn', 'goproxy.official'"
    for table, name, expression in (
        ("adapters", "ck_adapters_language", f"language IN ({languages})"),
        (
            "package_sources",
            "ck_package_sources_preset_id",
            f"preset_id IS NULL OR preset_id IN ({presets})",
        ),
        ("package_sources", "ck_package_sources_kind", f"kind IN ({kinds})"),
        ("builtin_packages", "ck_builtin_package", f"kind IN ({kinds}) AND size_bytes > 0"),
        ("builtin_package_uploads", "ck_builtin_upload", f"kind IN ({kinds}) AND size_bytes > 0"),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, expression)


def upgrade() -> None:
    _constraints(expanded=True)
    connection = op.get_bind()
    for preferred, url, preset, default in (
        ("DLR builtin source (goproxy)", "dlr-builtin://goproxy", None, False),
        ("Go 国内模块源", "https://goproxy.cn", "goproxy.cn", True),
        ("Go 官方模块源", "https://proxy.golang.org", "goproxy.official", False),
    ):
        name, suffix = preferred, 1
        while connection.scalar(
            sa.text("SELECT 1 FROM package_sources WHERE name = :name"), {"name": name}
        ):
            suffix += 1
            name = f"{preferred} ({suffix})"
        connection.execute(
            sa.text(
                "INSERT INTO package_sources (name, kind, index_url, is_default, preset_id) "
                "VALUES (:name, 'goproxy', :url, :default, :preset)"
            ),
            {"name": name, "url": url, "default": default, "preset": preset},
        )


def downgrade() -> None:
    # Refuse incompatible assets; never discard user content.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM user_templates,
                jsonb_array_elements(content->'variants') AS variant
                WHERE variant->>'language' IN ('typescript', 'go')) THEN
                RAISE EXCEPTION 'Remove TypeScript/Go user templates before downgrade';
            END IF;
        END $$
    """)
    _constraints(expanded=False)
