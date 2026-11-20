"""Per-feature readiness for storage. Other Control paths never probe file mounts."""

import asyncio
import hashlib
import json
import time
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from dlr.common.config import Settings, settings
from dlr.control import db
from dlr.control.ai.attachment_documents import DocumentConversionError, DocumentConverterClient
from dlr.control.services.control_declaration_v4 import DeclarationUnavailable, read_declaration
from dlr.control.services.shared_posix import (
    SharedNamespace,
    SharedStorageUnavailable,
    StoragePurpose,
)

_valid_until: dict[StoragePurpose, float] = {}


def configured(purpose: StoragePurpose, config: Settings = settings) -> SharedNamespace | None:
    root: str | Path
    match purpose:
        case "managed_files":
            backend, root, identity = (
                config.artifact_store_backend,
                config.artifact_store_root,
                config.artifact_store_namespace_id,
            )
        case "builtin_packages":
            backend, root, identity = (
                config.builtin_package_backend,
                config.builtin_package_root,
                config.builtin_package_namespace_id,
            )
        case "ai_attachments":
            backend, root, identity = (
                config.ai_attachment_backend,
                config.ai_attachment_root,
                config.ai_attachment_namespace_id,
            )
    if backend != "shared_posix" or identity is None or config.control_ha_slot is None:
        return None
    return SharedNamespace(
        Path(root), purpose, read_declaration(config).authority.cluster_id, identity
    )


def binding_generation(session: Session, namespace: SharedNamespace) -> int | None:
    """The deployment owner selects the namespace after verifying the copy manifest."""
    generation = session.scalar(
        text("""
            SELECT generation FROM control_storage_bindings
            WHERE purpose = :purpose AND deployment_id = :deployment
              AND namespace_id = :namespace AND state = 'active' AND policy_sha256=:policy
        """),
        {
            "purpose": namespace.purpose,
            "deployment": namespace.deployment_id,
            "namespace": namespace.namespace_id,
            "policy": policy_sha256(namespace.purpose),
        },
    )
    return generation if isinstance(generation, int) else None


def policy_sha256(purpose: StoragePurpose) -> str:
    policy: tuple[str | int, ...] = (purpose,)
    if purpose == "ai_attachments":
        policy += (
            settings.ai_attachment_ttl_seconds,
            settings.ai_attachment_store_max_bytes,
            settings.ai_attachment_derived_max_bytes,
        )
    return hashlib.sha256(json.dumps(policy, separators=(",", ":")).encode()).hexdigest()


def active_namespace(purpose: StoragePurpose, config: Settings = settings) -> SharedNamespace:
    namespace = configured(purpose, config)
    if namespace is None:
        raise SharedStorageUnavailable("Shared storage is not configured")
    with db.SessionLocal() as session:
        if binding_generation(session, namespace) is None:
            raise SharedStorageUnavailable("Shared storage is not active")
    return namespace


def available(purpose: StoragePurpose) -> bool:
    return settings.control_ha_slot is None or _valid_until.get(purpose, 0) > time.monotonic()


def bound(session: Session, purpose: StoragePurpose) -> bool:
    """Check the persisted generation in a request or an isolated transport child."""
    if settings.control_ha_slot is None:
        return True
    namespace = configured(purpose)
    return namespace is not None and binding_generation(session, namespace) is not None


def _probe(purpose: StoragePurpose) -> bool:
    try:
        namespace = active_namespace(purpose)
        with namespace.open(slot=settings.control_ha_slot):
            pass
        if purpose == "ai_attachments":
            if not settings.ai_document_converter_url:
                return False
            DocumentConverterClient(settings.ai_document_converter_url).identity()
    except (
        SQLAlchemyError,
        OSError,
        ValueError,
        RuntimeError,
        DeclarationUnavailable,
        DocumentConversionError,
    ):
        return False
    return True


async def monitor(purpose: StoragePurpose) -> None:
    """One outstanding probe per mount, including while a filesystem call is stuck."""
    try:
        while True:
            observed = time.monotonic()
            if await asyncio.to_thread(_probe, purpose):
                _valid_until[purpose] = observed + 10
            else:
                _valid_until.pop(purpose, None)
            await asyncio.sleep(2)
    finally:
        _valid_until.pop(purpose, None)
