"""Pydantic schemas for the M3.2 Secret Store (credentials and bindings).

Plaintext secret values only ever travel inside create/update request
bodies; no response ever contains them — responses carry metadata only.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

MAX_CREDENTIAL_NAME_LENGTH = 128

CredentialType = Literal["password", "token", "access_key", "secret"]


def _validate_name(value: object) -> str:
    """Trim an incoming credential name and enforce the length contract."""
    if not isinstance(value, str):
        raise ValueError("name must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError("name must not be blank")
    if len(stripped) > MAX_CREDENTIAL_NAME_LENGTH:
        raise ValueError(f"name must be at most {MAX_CREDENTIAL_NAME_LENGTH} characters")
    return stripped


class CredentialCreate(BaseModel):
    """Request body for POST /api/credentials.

    ``fields`` must carry exactly the fields of the credential type
    (e.g. ``username`` + ``password`` for type ``password``); values are
    encrypted at rest and never returned by any API afterwards.
    """

    name: str
    type: CredentialType
    fields: dict[str, str]

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        return _validate_name(value)


class CredentialUpdate(BaseModel):
    """Request body for PATCH /api/credentials/{credential_id}.

    Omitting ``fields`` keeps the stored ciphertext unchanged; sending new
    fields re-encrypts and replaces them.
    """

    name: str | None = None
    fields: dict[str, str] | None = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str | None:
        if value is None:
            return None
        return _validate_name(value)


class CredentialResponse(BaseModel):
    """Credential metadata; never contains plaintext or ciphertext."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: str
    created_at: datetime
    updated_at: datetime


class BindingItem(BaseModel):
    """One env_key -> credential field binding on an Adapter."""

    env_key: str
    credential_id: int
    field: str


class BindingsUpdate(BaseModel):
    """Request body for PUT /api/adapters/{adapter_id}/credential-bindings.

    Full replacement: the submitted list becomes the Adapter's complete
    binding set (an empty list clears all bindings).
    """

    bindings: list[BindingItem] = []


class BindingResponse(BindingItem):
    """Binding row enriched with credential metadata (never plaintext)."""

    credential_name: str
    credential_type: str
