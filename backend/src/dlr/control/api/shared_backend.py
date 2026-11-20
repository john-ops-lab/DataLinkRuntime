"""Feature gate for operations that need a shared file backend."""

from fastapi import HTTPException

from dlr.common.config import settings


def require_shared_backend() -> None:
    if settings.control_ha_slot is not None:
        raise HTTPException(status_code=503, detail={"code": "shared_backend_unavailable"})
