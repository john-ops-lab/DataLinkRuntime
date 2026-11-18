"""Append a new proof epoch with the Control offline and its owner fenced."""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from dlr.common.config import Settings
from dlr.common.schema_gate import acquire_migration_gate, try_control_gate
from dlr.control.services import control_cluster_identity as identity
from dlr.control.services import control_image_transition_v4 as images
from dlr.control.services.control_declaration_v4 import DeploymentDeclaration, read_private_json


class ProofRotationUnavailable(Exception):
    """Fixed diagnostic for a refused or unconfirmed authority transition."""


@dataclass(frozen=True)
class ProofRotation:
    operation_id: UUID
    image_transition_id: UUID | None
    evidence_sha256: bytes
    source: DeploymentDeclaration = field(repr=False)
    target: DeploymentDeclaration = field(repr=False)


def read_intent(
    path: str, source: DeploymentDeclaration, target: DeploymentDeclaration
) -> ProofRotation:
    try:
        value = read_private_json(path)
        if type(value) is not dict or set(value) != {
            "operation_id",
            "image_transition_id",
            "evidence_sha256",
        }:
            raise ValueError
        operation = UUID(value["operation_id"])
        image = (
            UUID(value["image_transition_id"]) if value["image_transition_id"] is not None else None
        )
        old, new = source.authority, target.authority
        identity._validated(old)
        identity._validated(new)
        if (
            operation.int == 0
            or str(operation) != value["operation_id"]
            or (
                image is not None and (image.int == 0 or str(image) != value["image_transition_id"])
            )
            or re.fullmatch(r"[0-9a-f]{64}", value["evidence_sha256"]) is None
            or source.authority_epoch.int == 0
            or target.authority_epoch.int == 0
            or source.authority_epoch == target.authority_epoch
            or old.worker_proof_public_key is None
            or new.worker_proof_public_key is None
            or old.worker_proof_public_key == new.worker_proof_public_key
            or old.image_digest != new.image_digest
            or any(
                getattr(old, key) != getattr(new, key)
                for key in images._UNCHANGED
                if key != "worker_proof_public_key"
            )
            or any(
                not hmac.compare_digest(getattr(old, key).encode(), getattr(new, key).encode())
                for key in ("master_key", "admin_token", "worker_token")
            )
        ):
            raise ValueError
        return ProofRotation(
            operation, image, bytes.fromhex(value["evidence_sha256"]), source, target
        )
    except Exception:
        raise ProofRotationUnavailable("control_proof_rotation_unavailable") from None


def _parameters(intent: ProofRotation) -> dict[str, object]:
    return {
        "operation": intent.operation_id,
        "source_epoch": intent.source.authority_epoch,
        "target_epoch": intent.target.authority_epoch,
        "source_key": intent.source.authority.worker_proof_public_key,
        "target_key": intent.target.authority.worker_proof_public_key,
        "image": intent.source.authority.image_digest,
        "head": intent.image_transition_id,
        "evidence": intent.evidence_sha256,
    }


def apply(session: Session, intent: ProofRotation, config: Settings) -> None:
    images._limits(session, readonly=False)
    acquire_migration_gate(session)
    images._owner_fence(session, config)
    if (
        session.scalar(
            text("SELECT operation_id FROM control_proof_rotations WHERE operation_id=:operation"),
            {"operation": intent.operation_id},
        )
        is not None
    ):
        # No search or repair by epoch; lookup of this exact receipt follows
        # in the independent observation transaction.
        return
    current = identity._authority(session, intent.source.authority, exclusive=True)
    if current["authority_epoch"] != intent.source.authority_epoch:
        raise ProofRotationUnavailable("control_proof_rotation_unavailable")
    images._clean(session)
    images._owner_fence(session, config)
    session.execute(
        text("""
        INSERT INTO control_proof_rotations (
          generation, operation_id, source_epoch, target_epoch, source_key,
          target_key, image_digest, image_transition_id, evidence_sha256, writer_xid
        )
        SELECT COALESCE(max(generation),0)+1, :operation, :source_epoch, :target_epoch,
          :source_key, :target_key, :image, :head, :evidence, pg_catalog.txid_current()
        FROM control_proof_rotations
        """),
        _parameters(intent),
    )
    session.execute(text("UPDATE control_process_instances SET revoked=true WHERE NOT revoked"))


def observe(session: Session, intent: ProofRotation) -> dict[str, object]:
    images._limits(session, readonly=True)
    try_control_gate(session)
    receipt = (
        session.execute(
            text("""
        SELECT r.generation, r.writer_xid, r.created_at,
              (r.generation = (SELECT max(generation) FROM control_proof_rotations)) AS current
        FROM control_proof_rotations r
        WHERE r.operation_id=:operation
          AND r.source_epoch=:source_epoch AND r.target_epoch=:target_epoch
          AND r.source_key=:source_key AND r.target_key=:target_key
          AND r.image_digest=:image AND r.image_transition_id IS NOT DISTINCT FROM :head
          AND r.evidence_sha256=:evidence
        """),
            _parameters(intent),
        )
        .mappings()
        .one_or_none()
    )
    if receipt is None:
        raise ProofRotationUnavailable("control_proof_rotation_unavailable")
    return {
        "status": "committed_current" if receipt["current"] else "committed_superseded",
        "operation_id": str(intent.operation_id),
        "generation": receipt["generation"],
        "writer_xid": receipt["writer_xid"],
        "committed_at": str(receipt["created_at"]),
        "target_epoch": str(intent.target.authority_epoch),
    }
