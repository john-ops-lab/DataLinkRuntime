"""M5.5.8 seed canonical dependency-source defaults on fresh deployments.

Fresh Compose deployments start with one default source per dependency
kind (PyPI / npm / Maven). The seeding is intentionally guarded by an
empty-table check so upgrading deployments keep their own sources.

Revision ID: 0013_m5_5_8_dep_source_defaults
Revises: 0012_m5_5_9_active_name_unique
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_m5_5_8_dep_source_defaults"
down_revision: str | None = "0012_m5_5_9_active_name_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_SOURCES = (
    (
        "阿里云 PyPI 镜像",
        "pypi",
        "https://mirrors.aliyun.com/pypi/simple/",
    ),
    (
        "npmmirror npm 镜像",
        "npm",
        "https://registry.npmmirror.com/",
    ),
    (
        "阿里云 Maven 公共仓库",
        "maven",
        "https://maven.aliyun.com/repository/public",
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    # Only a fresh deployment (no sources at all) receives the defaults;
    # upgrades with custom sources or with deliberately removed sources
    # keep their state, and restore is available in System Settings.
    existing = bind.execute(sa.text("SELECT count(*) FROM package_sources")).scalar()
    if existing:
        return
    # created_at / updated_at use the table's server defaults (now()).
    bind.execute(
        sa.text(
            "INSERT INTO package_sources (name, kind, index_url, is_default) "
            "VALUES (:name, :kind, :index_url, true)"
        ),
        [
            {"name": name, "kind": kind, "index_url": index_url}
            for name, kind, index_url in _DEFAULT_SOURCES
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    # Remove only the canonical seeded defaults; admin-created rows are kept.
    for name, kind, index_url in _DEFAULT_SOURCES:
        bind.execute(
            sa.text(
                "DELETE FROM package_sources WHERE name = :name AND kind = :kind "
                "AND index_url = :index_url"
            ),
            {"name": name, "kind": kind, "index_url": index_url},
        )
