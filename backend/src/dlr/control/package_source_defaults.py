"""Canonical platform-managed dependency source defaults.

This module is intentionally dependency-free so the service, migrations and
tests all consume the same system configuration without importing FastAPI or
database state from an Alembic revision.
"""

from typing import Final, NamedTuple


class PackageSourceDefault(NamedTuple):
    kind: str
    name: str
    index_url: str
    is_domestic: bool


DEFAULT_PACKAGE_SOURCES: Final[tuple[PackageSourceDefault, ...]] = (
    PackageSourceDefault(
        kind="pypi",
        name="阿里云 PyPI 镜像",
        index_url="https://mirrors.aliyun.com/pypi/simple/",
        is_domestic=True,
    ),
    PackageSourceDefault(
        kind="pypi",
        name="官方 PyPI",
        index_url="https://pypi.org/simple/",
        is_domestic=False,
    ),
    PackageSourceDefault(
        kind="npm",
        name="npmmirror npm 镜像",
        index_url="https://registry.npmmirror.com/",
        is_domestic=True,
    ),
    PackageSourceDefault(
        kind="npm",
        name="npm 官方源",
        index_url="https://registry.npmjs.org/",
        is_domestic=False,
    ),
    PackageSourceDefault(
        kind="maven",
        name="阿里云 Maven 公共仓库",
        index_url="https://maven.aliyun.com/repository/public",
        is_domestic=True,
    ),
    PackageSourceDefault(
        kind="maven",
        name="Maven Central",
        index_url="https://repo1.maven.org/maven2/",
        is_domestic=False,
    ),
)


def defaults_for_kind(kind: str) -> tuple[PackageSourceDefault, ...]:
    """Return the domestic and official defaults for one dependency kind."""
    return tuple(source for source in DEFAULT_PACKAGE_SOURCES if source.kind == kind)
