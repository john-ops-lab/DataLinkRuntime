"""Feature gate for operations that need a shared file backend."""

from fastapi import HTTPException

from dlr.control.services.shared_posix import StoragePurpose
from dlr.control.services.storage_backends import available


def require_backend(purpose: StoragePurpose) -> None:
    if not available(purpose):
        raise HTTPException(status_code=503, detail={"code": "shared_backend_unavailable"})


def require_managed_files() -> None:
    require_backend("managed_files")


def require_builtin_packages() -> None:
    require_backend("builtin_packages")


def require_ai_attachments() -> None:
    require_backend("ai_attachments")
