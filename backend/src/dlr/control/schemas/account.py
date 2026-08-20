"""Request and response contracts for account authentication and management."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

AccountRole = Literal["admin", "user"]


def _username(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("username must be a string")
    value = value.strip()
    if not value:
        raise ValueError("username must not be blank")
    return value


class AccountLogin(BaseModel):
    """Username/password login request."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(max_length=128)
    password: SecretStr = Field(min_length=1, max_length=256)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value: object) -> str:
        return _username(value)


class AccountPasswordChange(BaseModel):
    """Current-password verification plus the replacement password."""

    model_config = ConfigDict(extra="forbid")

    current_password: SecretStr = Field(min_length=1, max_length=256)
    new_password: SecretStr = Field(min_length=8, max_length=256)


class AccountPasswordReset(BaseModel):
    """Narrow superadmin emergency reset request."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(max_length=128)
    new_password: SecretStr = Field(min_length=8, max_length=256)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value: object) -> str:
        return _username(value)


class AccountUserCreate(BaseModel):
    """Administrator-created account; the first login must change its password."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(max_length=128)
    password: SecretStr = Field(min_length=8, max_length=256)
    role: AccountRole

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value: object) -> str:
        return _username(value)


class AccountUserPatch(BaseModel):
    """Mutable account profile and administrator-controlled lifecycle fields."""

    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(default=None, max_length=128)
    role: AccountRole | None = None
    enabled: bool | None = None

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value: object) -> str | None:
        if value is None:
            return None
        return _username(value)


class AccountUserPasswordReset(BaseModel):
    """Administrator password reset; the replacement is never returned."""

    model_config = ConfigDict(extra="forbid")

    new_password: SecretStr = Field(min_length=8, max_length=256)


class AccountUserResponse(BaseModel):
    """Secret-free account row exposed to an authorized administrator or owner."""

    model_config = ConfigDict(extra="forbid")

    id: int
    username: str
    role: AccountRole
    enabled: bool
    must_change_password: bool
    created_at: datetime
    updated_at: datetime


class AccountPrincipalResponse(BaseModel):
    """Non-secret current account identity exposed to the Web UI."""

    model_config = ConfigDict(extra="forbid")

    id: int
    username: str
    role: AccountRole
    enabled: bool
    must_change_password: bool


class AccountLoginResponse(BaseModel):
    """Successful account login result; the session itself is cookie-only."""

    model_config = ConfigDict(extra="forbid")

    principal: AccountPrincipalResponse


class AccountStatusResponse(BaseModel):
    """Secret-free status response for logout, reset and CSRF bootstrap."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"]


class AccountSessionResponse(BaseModel):
    """Compatibility shape for the current principal endpoint."""

    model_config = ConfigDict(extra="forbid")

    principal: AccountPrincipalResponse
