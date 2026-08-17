"""Pydantic schemas for platform-managed dependency sources."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator

MAX_PACKAGE_SOURCE_NAME_LENGTH = 128


def _validate_name(value: object) -> str:
    """Trim an incoming source name and enforce the length contract."""
    if not isinstance(value, str):
        raise ValueError("name must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError("name must not be blank")
    if len(stripped) > MAX_PACKAGE_SOURCE_NAME_LENGTH:
        raise ValueError(f"name must be at most {MAX_PACKAGE_SOURCE_NAME_LENGTH} characters")
    return stripped


class PackageSourceCreate(BaseModel):
    """Request body for POST /api/package-sources."""

    name: str
    kind: Literal["pypi", "npm", "maven"] = "pypi"
    index_url: str
    is_default: bool = False
    credential_id: int | None = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        return _validate_name(value)


class PackageSourceUpdate(BaseModel):
    """Request body for PATCH /api/package-sources/{package_source_id}.

    Omitted fields stay unchanged; an explicit ``credential_id: null``
    clears the credential reference.
    """

    name: str | None = None
    kind: Literal["pypi", "npm", "maven"] | None = None
    index_url: str | None = None
    is_default: bool | None = None
    credential_id: int | None = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str | None:
        if value is None:
            return None
        return _validate_name(value)


class PackageSourceResponse(BaseModel):
    """Package source metadata; the bound credential appears by name only."""

    id: int
    name: str
    # Stable system preset identity; null for user-created sources.
    preset_id: str | None = None
    kind: str
    index_url: str
    is_default: bool
    credential_id: int | None
    credential_name: str | None = None
    created_at: datetime
    updated_at: datetime


class ReachabilityResponse(BaseModel):
    """Result of a Control-side reachability probe against an index URL.

    Any HTTP answer (including 401/403 for authenticated indexes) counts
    as reachable; only transport failures are reported as unreachable.
    """

    ok: bool
    status_code: int | None = None
    error: str | None = None


class DefaultPackageSourceInfo(BaseModel):
    """Canonical fresh-deployment default for one dependency kind."""

    preset_id: str
    kind: str
    name: str
    index_url: str


class PackageSourceDefaultsResponse(BaseModel):
    """Canonical defaults for all three dependency kinds (M5.5.8)."""

    pypi: DefaultPackageSourceInfo
    npm: DefaultPackageSourceInfo
    maven: DefaultPackageSourceInfo
