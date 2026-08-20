"""SSE endpoint of the Control Node (admin-facing, M3 spec §7).

The browser must use ``fetch()`` with an ``Authorization`` header to read
this stream; the token never appears in the URL. Nginx disables proxy
buffering on this path so events are forwarded as they are produced.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from dlr.control import db
from dlr.control.security import Principal, require_business_principal, require_principal
from dlr.control.services import adapter_access
from dlr.control.services import events as events_service

router = APIRouter(dependencies=[Depends(require_business_principal)])
CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


@router.get("/api/executions/{execution_id}/events")
def execution_events(execution_id: int, principal: CurrentPrincipal) -> StreamingResponse:
    """Stream Execution state and log events until a terminal status."""
    # Validate before streaming starts: a 404 must arrive as a normal JSON
    # error, not as an exception inside an already-started response stream.
    # The pre-check uses an explicitly short-lived session instead of the
    # request-scoped dependency: FastAPI cleans up yield dependencies only
    # after the response finished, which would pin one useless DB connection
    # for the whole lifetime of every long-lived SSE stream.
    with db.SessionLocal() as session:
        adapter_access.require_execution_access(session, execution_id, principal, "read")
    return StreamingResponse(
        events_service.event_stream(execution_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
