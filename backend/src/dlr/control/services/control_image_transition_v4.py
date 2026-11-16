"""Explicit offline transition of one supported dedicated v4 release pair.

The operator owns physical stops, image provenance, broker/journal checks and
continuous exclusion of writers. These database checks do not prove those
external facts. No application lifespan or background service is started here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from dlr.common.config import Settings
from dlr.common.schema_gate import acquire_migration_gate, try_control_gate
from dlr.common.schema_version import SCHEMA_REVISION
from dlr.control.services import control_auth_proof, control_cluster_identity
from dlr.control.services.control_declaration_v4 import DeploymentDeclaration, read_private_json
from dlr.control.services.worker_remote_deployment_v4 import require_dedicated_deployment

SOURCE_SCHEMA = "0087_issue129_disposition"
SOURCE_SHA = "a37c9d3e19985acddb1f3e33ff6d3c7337147270"
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_FIELDS = {
    "operation_id",
    "previous_id",
    "source_schema",
    "target_schema",
    "source_sha",
    "target_sha",
    "evidence_sha256",
    "control_slots",
}
_UNCHANGED = (
    "cluster_id",
    "managed_fingerprint",
    "builtin_fingerprint",
    "attachment_fingerprint",
    "capacity_fingerprint",
    "worker_proof_public_key",
)


class TransitionUnavailable(Exception):
    """Stable secret-free failure; original transaction must be closed."""


@dataclass(frozen=True)
class ImageTransition:
    operation_id: UUID
    previous_id: UUID | None
    source: DeploymentDeclaration = field(repr=False)
    target: DeploymentDeclaration = field(repr=False)
    source_schema: str
    target_schema: str
    intent_sha256: str
    control_slots: int


def read_intent(
    path: str, source: DeploymentDeclaration, target: DeploymentDeclaration
) -> ImageTransition:
    """Bind the closed intent to both declarations, excluding private values."""
    try:
        value = read_private_json(path)
        if type(value) is not dict or set(value) != _FIELDS:
            raise ValueError
        operation = UUID(value["operation_id"])
        previous = UUID(value["previous_id"]) if value["previous_id"] is not None else None
        if (
            operation.int == 0
            or str(operation) != value["operation_id"]
            or (
                previous is not None
                and (previous.int == 0 or str(previous) != value["previous_id"])
            )
            or value["source_schema"] != SOURCE_SCHEMA
            or value["target_schema"] != SCHEMA_REVISION
            or value["source_sha"] != SOURCE_SHA
            or type(value["target_sha"]) is not str
            or _SHA.fullmatch(value["target_sha"]) is None
            or value["target_sha"] == SOURCE_SHA
            or type(value["evidence_sha256"]) is not str
            or _HEX.fullmatch(value["evidence_sha256"]) is None
            or type(value["control_slots"]) is not int
            or value["control_slots"] not in (1, 2)
            or previous is not None  # This first supported hop has no predecessor.
        ):
            raise ValueError
        for deployment in (source, target):
            control_cluster_identity._validated(deployment.authority)
            if deployment.authority_epoch.int == 0 or deployment.worker_proof_signing_key is None:
                raise ValueError
        if (
            source.authority_epoch != target.authority_epoch
            or source.authority.image_digest == target.authority.image_digest
            or any(getattr(source.authority, k) != getattr(target.authority, k) for k in _UNCHANGED)
            or source.worker_proof_signing_key != target.worker_proof_signing_key
            or any(
                not hmac.compare_digest(
                    getattr(source.authority, k).encode("utf-8"),
                    getattr(target.authority, k).encode("utf-8"),
                )
                for k in ("master_key", "admin_token", "worker_token")
            )
        ):
            raise ValueError
        public = dict(value)
        for label, deployment in (("source", source), ("target", target)):
            public_key = deployment.authority.worker_proof_public_key
            if public_key is None:
                raise ValueError
            public[label] = {
                k: str(getattr(deployment.authority, k))
                for k in _UNCHANGED
                if k != "worker_proof_public_key"
            } | {
                "authority_epoch": str(deployment.authority_epoch),
                "image_digest": deployment.authority.image_digest,
                "worker_proof_public_key": public_key.hex(),
            }
        digest = hashlib.sha256(
            json.dumps(public, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return ImageTransition(
            operation,
            previous,
            source,
            target,
            SOURCE_SCHEMA,
            SCHEMA_REVISION,
            digest,
            value["control_slots"],
        )
    except Exception:
        raise TransitionUnavailable("image_transition_intent_unavailable") from None


def _limits(session: Session, *, readonly: bool) -> None:
    if session.in_transaction() or session.get_bind().dialect.name != "postgresql":
        raise TransitionUnavailable("image_transition_transaction_unavailable")
    if readonly:
        session.execute(text("SET TRANSACTION READ ONLY"))
    if session.scalar(text("SHOW transaction_isolation")) != "read committed":
        raise TransitionUnavailable("image_transition_transaction_unavailable")
    session.execute(text("SET LOCAL statement_timeout = '10000ms'"))
    session.execute(text("SET LOCAL lock_timeout = '5000ms'"))


def _owner_fence(session: Session, config: Settings) -> None:
    """A single owner connection, runtime NOLOGIN, no other live client/xact."""
    role, admins = config.worker_v4_database_role, config.worker_v4_database_admin_roles
    if (
        config.worker_remote_v4_enabled is not True
        or type(role) is not str
        or not role
        or len(role.encode()) > 63
        or "\x00" in role
        or type(admins) is not list
        or len(admins) > 16
        or role in admins
        or any(type(r) is not str or not r or len(r.encode()) > 63 or "\x00" in r for r in admins)
        or session.get_bind().dialect.name != "postgresql"
    ):
        raise TransitionUnavailable("image_transition_owner_unavailable")
    session.execute(text("SELECT pg_catalog.pg_stat_clear_snapshot()"))
    permitted = session.scalar(
        text("""
        SELECT current_user = session_user AND current_user <> :runtime
          AND current_user = pg_catalog.pg_get_userbyid(
            (SELECT relowner FROM pg_catalog.pg_class
             WHERE oid = 'control_cluster_authority'::regclass))
          AND EXISTS (SELECT 1 FROM pg_catalog.pg_roles
                      WHERE rolname = :runtime AND NOT rolcanlogin)
          AND NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolcanlogin
              AND r.rolname <> current_user AND r.rolname <> ALL(CAST(:admins AS text[]))
              AND pg_catalog.has_database_privilege(r.oid, current_database(), 'CONNECT'))
          AND NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_stat_activity
            WHERE datname = current_database() AND pid <> pg_catalog.pg_backend_pid()
              AND usesysid IS NOT NULL)
          AND NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_prepared_xacts WHERE database = current_database())
    """),
        {"runtime": role, "admins": admins},
    )
    if permitted is not True:
        raise TransitionUnavailable("image_transition_owner_unavailable")


def _owner_authorized(session: Session, config: Settings) -> None:
    if (
        config.worker_remote_v4_enabled is not True
        or session.scalar(
            text("""
        SELECT current_user = session_user AND current_user <> :runtime
          AND current_user = pg_catalog.pg_get_userbyid(
            (SELECT relowner FROM pg_catalog.pg_class
             WHERE oid = 'control_cluster_authority'::regclass))
          AND current_user = pg_catalog.pg_get_userbyid(
            (SELECT relowner FROM pg_catalog.pg_class
             WHERE oid = 'control_image_transitions'::regclass))
    """),
            {"runtime": config.worker_v4_database_role},
        )
        is not True
    ):
        raise TransitionUnavailable("image_transition_owner_unavailable")


def _identity_matches(row: Any, deployment: DeploymentDeclaration) -> bool:
    d = deployment.authority
    return bool(
        row is not None
        and row["authority_epoch"] == deployment.authority_epoch
        and all(row[k] == getattr(d, k) for k in _UNCHANGED)
        and control_auth_proof.verify_auth_proof(
            control_auth_proof.ControlAuthProof(
                row["master_check"], row["admin_check"], row["worker_check"]
            ),
            cluster_id=d.cluster_id,
            master_key=d.master_key,
            admin_token=d.admin_token,
            worker_token=d.worker_token,
        )
    )


def _authority_matches(row: Any, deployment: DeploymentDeclaration, schema: str) -> bool:
    d = deployment.authority
    return bool(
        row is not None
        and _identity_matches(row, deployment)
        and row["schema_revision"] == schema
        and row["image_digest"] == d.image_digest
    )


def _clean(session: Session) -> None:
    require_dedicated_deployment(session, readonly=True)
    clean = session.scalar(
        text("""
        SELECT NOT EXISTS (SELECT 1 FROM adapter_schedules WHERE enabled)
          AND NOT EXISTS (SELECT 1 FROM workers WHERE scheduling_state <> 'disabled'
            OR (instance_id IS NOT NULL AND instance_lease_until >= clock_timestamp()))
          AND NOT EXISTS (SELECT 1 FROM control_process_instances
                          WHERE lease_until >= clock_timestamp())
          AND NOT EXISTS (SELECT 1 FROM worker_lease_candidates_v4
                          WHERE guard_until >= clock_timestamp()
                             OR nominal_until >= clock_timestamp())
          AND NOT EXISTS (SELECT 1 FROM attempt_lease_candidates_v4
                          WHERE guard_until >= clock_timestamp()
                             OR nominal_until >= clock_timestamp())
          AND NOT EXISTS (SELECT 1 FROM executions
                          WHERE status NOT IN ('succeeded','dead_letter','cancelled','expired')
                            OR admission_released_at IS NULL)
          AND NOT EXISTS (SELECT 1 FROM adapter_execution_admission
                          WHERE outstanding_count <> 0 OR outstanding_bytes <> 0)
          AND NOT EXISTS (SELECT 1 FROM global_execution_admission
                          WHERE outstanding_count <> 0 OR outstanding_bytes <> 0)
          AND NOT EXISTS (SELECT 1 FROM execution_attempts
                          WHERE status IN ('claimed','running') OR cleanup_settled_at IS NULL
                            OR recovery_not_before IS NULL
                            OR recovery_not_before >= clock_timestamp()
                            OR lease_expires_at >= clock_timestamp())
          AND NOT EXISTS (SELECT 1 FROM adapter_execution_slots WHERE active_attempt_id IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM execution_input_artifact_leases)
          AND NOT EXISTS (SELECT 1 FROM execution_outbox
                          WHERE status <> 'published' OR lease_owner IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM execution_infrastructure_incidents WHERE status = 'open')
          AND NOT EXISTS (SELECT 1 FROM worker_cleanup_intents_v4
                          WHERE completion_id IS NULL OR proof_before >= clock_timestamp())
          AND NOT EXISTS (SELECT 1 FROM attempt_result_intents_v4
                          WHERE finalization_id IS NULL OR proof_before >= clock_timestamp())
          AND NOT EXISTS (SELECT 1 FROM worker_cleanup_requests WHERE status <> 'completed')
    """)
    )
    if clean is not True:
        raise TransitionUnavailable("image_transition_unsettled")


def _revision(session: Session) -> str:
    versions = session.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(versions) != 1 or versions[0] not in (SOURCE_SCHEMA, SCHEMA_REVISION):
        raise TransitionUnavailable("image_transition_schema_unavailable")
    return str(versions[0])


def _receipt_matches(row: Any, intent: ImageTransition) -> bool:
    return bool(
        row["intent_sha256"] == intent.intent_sha256
        and row["previous_id"] == intent.previous_id
        and row["cluster_id"] == intent.source.authority.cluster_id
        and row["authority_epoch"] == intent.source.authority_epoch
        and row["source_digest"] == intent.source.authority.image_digest
        and row["target_digest"] == intent.target.authority.image_digest
        and row["source_schema"] == intent.source_schema
        and row["target_schema"] == intent.target_schema
        and row["writer_xid"] > 0
    )


def observe(session: Session, intent: ImageTransition) -> dict[str, object]:
    """One independent read-only snapshot; no pre-commit row counts as proof."""
    _limits(session, readonly=True)
    try_control_gate(session)
    revision = _revision(session)
    if revision == SOURCE_SCHEMA:
        if session.scalar(text("SELECT to_regclass('control_image_transitions')")) is not None:
            raise TransitionUnavailable("image_transition_schema_unavailable")
        return {"status": "not_installed", "operation_id": str(intent.operation_id)}
    # All receipt/current-chain/authority facts share one statement snapshot.
    row = (
        session.execute(
            text("""
        SELECT row_to_json(r) AS receipt, a.*, latest.operation_id AS latest_id,
               latest.target_digest AS latest_digest, latest.target_schema AS latest_schema
        FROM (SELECT 1) seed
        LEFT JOIN control_image_transitions r ON r.operation_id = :operation
        LEFT JOIN control_cluster_authority a ON a.singleton = 1
        LEFT JOIN control_image_transitions latest
          ON NOT EXISTS (SELECT 1 FROM control_image_transitions n
                         WHERE n.previous_id = latest.operation_id)
    """),
            {"operation": intent.operation_id},
        )
        .mappings()
        .one()
    )
    receipt = row["receipt"]
    if receipt is None:
        legal = (
            _authority_matches(row, intent.source, intent.source_schema)
            if row["latest_id"] is None
            else (
                _identity_matches(row, intent.target)
                and row["image_digest"] == row["latest_digest"]
                and row["schema_revision"] == row["latest_schema"]
            )
        )
        if not legal:
            raise TransitionUnavailable("image_transition_conflict")
        return {"status": "not_found", "operation_id": str(intent.operation_id)}
    for k in ("operation_id", "previous_id", "cluster_id", "authority_epoch"):
        receipt[k] = UUID(receipt[k]) if receipt[k] is not None else None
    if not _receipt_matches(receipt, intent):
        raise TransitionUnavailable("image_transition_conflict")
    if (
        not _identity_matches(row, intent.target)
        or row["latest_id"] is None
        or row["image_digest"] != row["latest_digest"]
        or row["schema_revision"] != row["latest_schema"]
    ):
        raise TransitionUnavailable("image_transition_conflict")
    current = bool(
        row["latest_id"] == intent.operation_id
        and _authority_matches(row, intent.target, intent.target_schema)
    )
    if row["latest_id"] == intent.operation_id and not current:
        raise TransitionUnavailable("image_transition_conflict")
    return {
        "status": "committed_current" if current else "committed_superseded",
        "operation_id": str(intent.operation_id),
        "intent_sha256": intent.intent_sha256,
        "writer_xid": receipt["writer_xid"],
        "committed_at": receipt["committed_at"],
    }


def check(session: Session, intent: ImageTransition, config: Settings) -> str:
    """Offline eligibility only; never writes authority, roles or a receipt."""
    if config.worker_v4_control_slots != intent.control_slots:
        raise TransitionUnavailable("image_transition_capacity_unavailable")
    _limits(session, readonly=True)
    try_control_gate(session)
    _owner_fence(session, config)
    revision = _revision(session)
    row = (
        session.execute(text("SELECT * FROM control_cluster_authority WHERE singleton=1"))
        .mappings()
        .one_or_none()
    )
    if revision == SCHEMA_REVISION:
        _owner_authorized(session, config)
        receipt = (
            session.execute(
                text("SELECT * FROM control_image_transitions WHERE operation_id=:id"),
                {"id": intent.operation_id},
            )
            .mappings()
            .one_or_none()
        )
        if receipt is not None:
            if not _receipt_matches(receipt, intent) or not _authority_matches(
                row, intent.target, intent.target_schema
            ):
                raise TransitionUnavailable("image_transition_conflict")
            latest = session.scalar(
                text(
                    "SELECT operation_id FROM control_image_transitions t WHERE NOT EXISTS "
                    "(SELECT 1 FROM control_image_transitions n "
                    "WHERE n.previous_id=t.operation_id)"
                )
            )
            if latest != intent.operation_id:
                raise TransitionUnavailable("image_transition_conflict")
            return "committed_current"
        if session.scalar(text("SELECT EXISTS (SELECT 1 FROM control_image_transitions)")):
            raise TransitionUnavailable("image_transition_conflict")
    elif session.scalar(text("SELECT to_regclass('control_image_transitions')")) is not None:
        raise TransitionUnavailable("image_transition_schema_unavailable")
    if not _authority_matches(row, intent.source, intent.source_schema):
        raise TransitionUnavailable("image_transition_authority_unavailable")
    _clean(session)
    return "source_checked" if revision == SOURCE_SCHEMA else "ready_to_apply"


def apply(session: Session, intent: ImageTransition, config: Settings) -> None:
    """Stage one top-level transaction. Caller commits, closes, then observes."""
    if session.in_transaction() or config.worker_v4_control_slots != intent.control_slots:
        raise TransitionUnavailable("image_transition_transaction_unavailable")
    _limits(session, readonly=False)
    acquire_migration_gate(session)
    if _revision(session) != SCHEMA_REVISION:
        raise TransitionUnavailable("image_transition_schema_unavailable")
    _owner_authorized(session, config)
    row = (
        session.execute(
            text("SELECT * FROM control_cluster_authority WHERE singleton=1 FOR UPDATE")
        )
        .mappings()
        .one_or_none()
    )
    receipt = (
        session.execute(
            text("SELECT * FROM control_image_transitions WHERE operation_id=:id FOR SHARE"),
            {"id": intent.operation_id},
        )
        .mappings()
        .one_or_none()
    )
    if receipt is not None:
        if not _receipt_matches(receipt, intent) or not _identity_matches(row, intent.target):
            raise TransitionUnavailable("image_transition_conflict")
        # Retried committed operations perform no fencing, even after restart.
        return
    _owner_fence(session, config)
    if session.scalar(text("SELECT EXISTS (SELECT 1 FROM control_image_transitions)")):
        raise TransitionUnavailable("image_transition_conflict")
    if not _authority_matches(row, intent.source, intent.source_schema):
        raise TransitionUnavailable("image_transition_authority_unavailable")
    session.execute(
        text("SELECT instance_id FROM control_process_instances ORDER BY instance_id FOR UPDATE")
    ).all()
    _clean(session)
    _owner_fence(session, config)
    changed = session.execute(
        text("""
        UPDATE control_cluster_authority SET image_digest=:target, schema_revision=:schema
        WHERE singleton=1 AND image_digest=:source AND schema_revision=:source_schema
        RETURNING singleton
    """),
        {
            "target": intent.target.authority.image_digest,
            "schema": intent.target_schema,
            "source": intent.source.authority.image_digest,
            "source_schema": intent.source_schema,
        },
    )
    if changed.scalar_one_or_none() != 1:
        raise TransitionUnavailable("image_transition_conflict")
    session.execute(
        text("""
        INSERT INTO control_image_transitions
          (operation_id, cluster_id, authority_epoch, previous_id, source_digest,
           target_digest, source_schema, target_schema, intent_sha256, writer_xid, committed_at)
        VALUES (:id, :cluster, :epoch, :previous, :source, :target, :source_schema,
                :target_schema, :intent, pg_catalog.txid_current(), pg_catalog.clock_timestamp())
    """),
        {
            "id": intent.operation_id,
            "cluster": intent.source.authority.cluster_id,
            "epoch": intent.source.authority_epoch,
            "previous": intent.previous_id,
            "source": intent.source.authority.image_digest,
            "target": intent.target.authority.image_digest,
            "source_schema": intent.source_schema,
            "target_schema": intent.target_schema,
            "intent": intent.intent_sha256,
        },
    )
    session.execute(text("UPDATE control_process_instances SET revoked=true WHERE NOT revoked"))
