"""Administrator API for the fixed, read-only Tencent ima KnowledgeSource."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.schemas.knowledge_source import (
    KnowledgeBaseResponse,
    KnowledgeSourceResponse,
    KnowledgeSourceTestResponse,
    KnowledgeSourceUpdate,
)
from dlr.control.security import require_admin_principal
from dlr.control.services import knowledge_source as knowledge_source_service

router = APIRouter(dependencies=[Depends(require_admin_principal)])

DbSession = Annotated[Session, Depends(db.get_session)]


@router.get("/api/knowledge-sources", response_model=list[KnowledgeSourceResponse])
def list_knowledge_sources(session: DbSession) -> list[KnowledgeSourceResponse]:
    return knowledge_source_service.list_settings(session)


@router.get("/api/knowledge-sources/{source_id}", response_model=KnowledgeSourceResponse)
def get_knowledge_source(source_id: str, session: DbSession) -> KnowledgeSourceResponse:
    return knowledge_source_service.setting_response(session, source_id)


@router.put("/api/knowledge-sources/{source_id}", response_model=KnowledgeSourceResponse)
def put_knowledge_source(
    source_id: str, payload: KnowledgeSourceUpdate, session: DbSession
) -> KnowledgeSourceResponse:
    return knowledge_source_service.save_setting(session, payload, source_id)


@router.post("/api/knowledge-sources/{source_id}/test", response_model=KnowledgeSourceTestResponse)
def test_knowledge_source(source_id: str, session: DbSession) -> KnowledgeSourceTestResponse:
    return knowledge_source_service.test_connection(session, source_id)


@router.post(
    "/api/knowledge-sources/{source_id}/validate", response_model=KnowledgeSourceTestResponse
)
def validate_knowledge_source(source_id: str, session: DbSession) -> KnowledgeSourceTestResponse:
    """Compatibility alias for clients that label the action "Validate"."""
    return knowledge_source_service.test_connection(session, source_id)


@router.get(
    "/api/knowledge-sources/{source_id}/knowledge-bases",
    response_model=list[KnowledgeBaseResponse],
)
def list_accessible_knowledge_bases(
    source_id: str, session: DbSession
) -> list[KnowledgeBaseResponse]:
    return knowledge_source_service.list_knowledge_bases(session, source_id)
