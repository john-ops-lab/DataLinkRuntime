"""Health check endpoints of the Control Node."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from dlr.control import db

router = APIRouter()


@router.get("/api/health")
def health() -> JSONResponse:
    """Report Control Node status, including database reachability."""
    database_ok = db.check_database()
    payload = {
        "service": "dlr-control",
        "status": "ok" if database_ok else "degraded",
        "database": database_ok,
    }
    return JSONResponse(content=payload, status_code=200 if database_ok else 503)
