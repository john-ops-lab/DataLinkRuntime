"""SSE endpoint of the Control Node (admin-facing, M3 spec §7).

The browser must use ``fetch()`` with an ``Authorization`` header to read
this stream; the token never appears in the URL. Nginx disables proxy
buffering on this path so events are forwarded as they are produced.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from dlr.control import db
from dlr.control.security import require_admin_token
from dlr.control.services import events as events_service
from dlr.control.services import execution as execution_service

router = APIRouter(dependencies=[Depends(require_admin_token)])

DbSession = Annotated[Session, Depends(db.get_session)]


@router.get("/api/executions/{execution_id}/events")
def execution_events(execution_id: int, session: DbSession) -> StreamingResponse:
    """Stream Execution state and log events until a terminal status."""
    # Validate before streaming starts: a 404 must arrive as a normal JSON
    # error, not as an exception inside an already-started response stream.
    execution_service.get_execution(session, execution_id)
    return StreamingResponse(
        events_service.event_stream(execution_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
