"""Per-feature readiness for storage. Other Control paths never probe file mounts."""

import asyncio
import hashlib
import json
import time
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from dlr.common.config import Settings, settings
from dlr.control import db
from dlr.control.ai.attachment_documents import DocumentConversionError, DocumentConverterClient
from dlr.control.services import control_ha_capacity
from dlr.control.services.control_declaration_v4 import DeclarationUnavailable, read_declaration
from dlr.control.services.shared_posix import (
    SharedNamespace,
    SharedStorageUnavailable,
    StoragePurpose,
    preflight_id,
    runtime_mount_id,
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
            converter_url("a") or "",
            converter_url("b") or "",
        )
    return hashlib.sha256(json.dumps(policy, separators=(",", ":")).encode()).hexdigest()


def converter_url(slot: str) -> str | None:
    if slot == settings.control_ha_slot:
        return settings.ai_document_converter_url
    return settings.ai_attachment_peer_converter_url or settings.ai_document_converter_url


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
        slot = settings.control_ha_slot
        if slot is None:
            return False
        namespace = active_namespace(purpose)
        with namespace.open(slot=slot) as directory:
            proof = preflight_id(directory)
            mounted = runtime_mount_id(directory)
        if purpose == "ai_attachments":
            if not settings.ai_document_converter_url:
                return False
            DocumentConverterClient(settings.ai_document_converter_url).identity()
        return _observe_mount(namespace, slot, proof, mounted)
    except (
        SQLAlchemyError,
        OSError,
        ValueError,
        RuntimeError,
        DeclarationUnavailable,
        DocumentConversionError,
    ):
        return False


def _observe_mount(namespace: SharedNamespace, slot: str, proof: UUID, mounted: int) -> bool:
    with db.SessionLocal.begin() as session:
        instance = control_ha_capacity.assert_local_control(session)
        generation = binding_generation(session, namespace)
        if instance is None or generation is None:
            return False
        params = {
            "purpose": namespace.purpose,
            "slot": slot,
            "namespace": namespace.namespace_id,
            "generation": generation,
            "instance": instance,
            "mount": mounted,
            "proof": proof,
        }
        previous = session.execute(
            text("""
            SELECT namespace_id,generation,instance_id,mount_id,proof_id
            FROM control_storage_observations WHERE purpose=:purpose AND slot=:slot FOR UPDATE
        """),
            params,
        ).one_or_none()
        if (
            previous is not None
            and previous.namespace_id == namespace.namespace_id
            and previous.generation == generation
            and previous.proof_id == proof
        ):
            return (previous.instance_id, previous.mount_id) == (instance, mounted)
        session.execute(
            text("""
            INSERT INTO control_storage_observations
              (purpose,slot,namespace_id,generation,instance_id,mount_id,proof_id)
            VALUES (:purpose,:slot,:namespace,:generation,:instance,:mount,:proof)
            ON CONFLICT (purpose,slot) DO UPDATE SET namespace_id=excluded.namespace_id,
              generation=excluded.generation, instance_id=excluded.instance_id,
              mount_id=excluded.mount_id, proof_id=excluded.proof_id
        """),
            params,
        )
        control_ha_capacity.recheck_local_control(session)
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
