"""Credential (Secret Store) and Adapter binding endpoints of the Control Node.

Global Credential CRUD is admin-only. Adapter binding metadata is readable
through the Adapter ACL, while credential choices and binding writes remain
owner/admin-only. Responses carry metadata only: plaintext field values are
accepted in create/update bodies and never returned afterwards.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.credential import (
    BindingResponse,
    BindingsUpdate,
    CredentialCreate,
    CredentialResponse,
    CredentialUpdate,
)
from dlr.control.security import (
    Principal,
    require_admin_principal,
    require_business_principal,
    require_principal,
)
from dlr.control.services import adapter_access
from dlr.control.services import secrets as secrets_service

router = APIRouter(dependencies=[Depends(require_admin_principal)])
adapter_router = APIRouter(dependencies=[Depends(require_business_principal)])

DbSession = Annotated[Session, Depends(db.get_session)]
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


@router.get("/api/credentials", response_model=list[CredentialResponse])
def list_credentials(session: DbSession) -> list[CredentialResponse]:
    credentials = secrets_service.list_credentials(session)
    return [CredentialResponse.model_validate(credential) for credential in credentials]


@router.post("/api/credentials", status_code=201, response_model=CredentialResponse)
def create_credential(payload: CredentialCreate, session: DbSession) -> CredentialResponse:
    return CredentialResponse.model_validate(secrets_service.create_credential(session, payload))


@router.get("/api/credentials/{credential_id}", response_model=CredentialResponse)
def get_credential(credential_id: int, session: DbSession) -> CredentialResponse:
    return CredentialResponse.model_validate(secrets_service.get_credential(session, credential_id))


@router.patch("/api/credentials/{credential_id}", response_model=CredentialResponse)
def update_credential(
    credential_id: int, payload: CredentialUpdate, session: DbSession
) -> CredentialResponse:
    return CredentialResponse.model_validate(
        secrets_service.update_credential(session, credential_id, payload)
    )


@router.delete("/api/credentials/{credential_id}", status_code=204)
def delete_credential(credential_id: int, session: DbSession) -> Response:
    secrets_service.delete_credential(session, credential_id)
    return Response(status_code=204)


@adapter_router.get(
    "/api/adapters/{adapter_id}/credential-bindings", response_model=list[BindingResponse]
)
def list_adapter_bindings(
    adapter_id: int, principal: CurrentPrincipal, session: DbSession
) -> list[BindingResponse]:
    adapter_access.require_adapter_access(session, adapter_id, principal, "read")
    return secrets_service.list_adapter_bindings(session, adapter_id)


@adapter_router.get(
    "/api/adapters/{adapter_id}/credential-options", response_model=list[CredentialResponse]
)
def list_adapter_credential_options(
    adapter_id: int, principal: CurrentPrincipal, session: DbSession
) -> list[CredentialResponse]:
    """Return metadata-only Credential choices for an owner/admin binding UI."""
    adapter_access.require_adapter_access(session, adapter_id, principal, "owner")
    return [
        CredentialResponse.model_validate(credential)
        for credential in secrets_service.list_credentials(session)
    ]


@adapter_router.put(
    "/api/adapters/{adapter_id}/credential-bindings", response_model=list[BindingResponse]
)
def set_adapter_bindings(
    adapter_id: int,
    payload: BindingsUpdate,
    principal: CurrentPrincipal,
    session: DbSession,
) -> list[BindingResponse]:
    """Full replacement of the Adapter's binding set."""
    adapter_access.require_adapter_access(session, adapter_id, principal, "owner")
    items = [(item.env_key, item.credential_id, item.field) for item in payload.bindings]
    return secrets_service.set_adapter_bindings(session, adapter_id, items)
