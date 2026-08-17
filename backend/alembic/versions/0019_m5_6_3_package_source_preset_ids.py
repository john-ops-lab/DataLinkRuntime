"""Persist stable identities for genuine dependency-source presets.

Revision ID: 0019_m5_6_3_preset_ids
Revises: 0018_m5_6_2_execution_locale
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_m5_6_3_preset_ids"
down_revision: str | None = "0018_m5_6_2_execution_locale"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRESETS = (
    ("pypi.aliyun", "pypi", "阿里云 PyPI 镜像", "https://mirrors.aliyun.com/pypi/simple/"),
    ("pypi.official", "pypi", "官方 PyPI", "https://pypi.org/simple/"),
    ("npm.npmmirror", "npm", "npmmirror npm 镜像", "https://registry.npmmirror.com/"),
    ("npm.official", "npm", "npm 官方源", "https://registry.npmjs.org/"),
    (
        "maven.aliyun",
        "maven",
        "阿里云 Maven 公共仓库",
        "https://maven.aliyun.com/repository/public",
    ),
    ("maven.central", "maven", "Maven Central", "https://repo1.maven.org/maven2/"),
)


def upgrade() -> None:
    op.add_column("package_sources", sa.Column("preset_id", sa.String(length=64), nullable=True))
    op.create_check_constraint(
        "ck_package_sources_preset_id",
        "package_sources",
        "preset_id IS NULL OR preset_id IN ("
        "'pypi.aliyun', 'pypi.official', 'npm.npmmirror', 'npm.official', "
        "'maven.aliyun', 'maven.central')",
    )
    # Historical seeded rows use their canonical display name. User-created
    # rows with the same URL but a different name remain intentionally NULL.
    connection = op.get_bind()
    for preset_id, kind, name, index_url in _PRESETS:
        connection.execute(
            sa.text(
                "UPDATE package_sources SET preset_id = :preset_id "
                "WHERE kind = :kind AND name = :name AND index_url = :index_url"
            ),
            {
                "preset_id": preset_id,
                "kind": kind,
                "name": name,
                "index_url": index_url,
            },
        )


def downgrade() -> None:
    op.drop_constraint("ck_package_sources_preset_id", "package_sources", type_="check")
    op.drop_column("package_sources", "preset_id")
