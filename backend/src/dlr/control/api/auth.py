"""Authentication helper endpoints of the Control Node."""

from fastapi import APIRouter, Depends

from dlr.control.security import require_admin_token

router = APIRouter()


@router.get("/api/auth/admin/verify")
def verify_admin_token(_: None = Depends(require_admin_token)) -> dict[str, str]:
    """Minimal probe the Web UI uses to validate an admin token."""
    return {"status": "ok"}
