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
LEGACY_SCHEMA = "0088_issue129_image_transition"
J_SCHEMA = "0089_issue129_credential_ops"
W1_SCHEMA = "0090_issue129_worker_cutover"
W2_ROUTING_SCHEMA = "0091_issue129_pool_routing"
W2_CAPACITY_SCHEMA = "0092_issue129_worker_capacity"
W2_PLACEMENT_SCHEMA = "0093_issue129_worker_placement"
W3_FOUNDATION_SCHEMA = "0094_issue129_control_ha"
W3_TAKEOVER_SCHEMA = "0095_issue129_takeover"
SOURCE_SHA = "a37c9d3e19985acddb1f3e33ff6d3c7337147270"
F_SOURCE_SHA = "0417b82c4ab6f76aefdf3eafe38327f46cb18358"
# Verified F build inputs from its original delivery. Product provenance and
# actual image digests remain an offline operator responsibility.
_F_RELEASE_SHAS = frozenset(
    {
        F_SOURCE_SHA,
        "49269698f63cd85128f16f60c0ad2ba684d55902",
        "fd7b7d2c4064c8ff55d713a5d9324823afb14c2a",
    }
)
G_SOURCE_SHA = "a4a9da204659c36ec6b843bb28c8f13e5f355582"
_G_RELEASE_SHAS = frozenset(
    {
        G_SOURCE_SHA,
        "530ab2e8e5e7efaa7a1492a2c754a461cb6d4ba7",
        "8d216d3a052a6643fdf1bee74b1538e1ddd8a891",
    }
)
H_SOURCE_SHA = "9d7cd67f483950d4a6f96ebc9342005636f61348"
_H_RELEASE_SHAS = frozenset(
    {
        H_SOURCE_SHA,
        "ed925887f28704001117825d0d46488dacbfe595",
        "72f2aa23bd9b680f820d16b82283b950533c6775",
    }
)
I_SOURCE_SHA = "c9b21a4ae3f05c4e382e070fa180dbd07f58d350"
_I_RELEASE_SHAS = frozenset(
    {
        I_SOURCE_SHA,
        "227cc637292f23cb8432cd10ba24f06a9090546e",
        "4e513d1ceb34999c45c9ca42f08135704f72faa7",
    }
)
J_SOURCE_SHA = "6c356267ee1052426954d2e7a1617aff87f3c3a0"
_J_RELEASE_SHAS = frozenset(
    {
        J_SOURCE_SHA,
        "7ec83378a04fff249e49586d75bade9ca821677d",
        "4a057c7d9eae84b563bb952837e12e90095c7c44",
    }
)
W1_SOURCE_SHA = "347be42970a765fc08557528991134d60c79dcae"
_W1_RELEASE_SHAS = frozenset({W1_SOURCE_SHA, "bbfedfa93f37f9c008084085e8bbb7998fba8897"})
W2_ROUTING_SOURCE_SHA = "f536419b59282891cf537e2d8fef65b7e8b92d1c"
_W2_ROUTING_RELEASE_SHAS = frozenset({W2_ROUTING_SOURCE_SHA})
W2_CAPACITY_SOURCE_SHA = "36ee472851e2141ce908dcd9a9588c5f6a86f9eb"
_W2_CAPACITY_RELEASE_SHAS = frozenset({W2_CAPACITY_SOURCE_SHA})
W2_PLACEMENT_SOURCE_SHA = "9b3347578b1644706731e5f7c5c1461385b71741"
_W2_PLACEMENT_RELEASE_SHAS = frozenset({W2_PLACEMENT_SOURCE_SHA})
W3_FOUNDATION_SOURCE_SHA = "686f6db768e48a63ca29229a713cf39ecce99a5d"
_W3_FOUNDATION_RELEASE_SHAS = frozenset({W3_FOUNDATION_SOURCE_SHA})
W3_TAKEOVER_SOURCE_SHA = "1fc2ca9483a32d607a27cc9942a1a92a29ba68d3"
_W3_TAKEOVER_RELEASE_SHAS = frozenset({W3_TAKEOVER_SOURCE_SHA})
_MAX_TRANSITIONS = 12
_WRITABLE_TRANSITIONS = {4, 5, 6, 7, 8, 9, 10, 11}
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
    source_sha: str
    target_sha: str
    predecessor: ImageTransition | None = field(default=None, repr=False)


def read_intent(
    path: str,
    source: DeploymentDeclaration,
    target: DeploymentDeclaration,
    *,
    predecessor: ImageTransition | None = None,
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
            or value["target_schema"]
            not in (
                LEGACY_SCHEMA,
                J_SCHEMA,
                W1_SCHEMA,
                W2_ROUTING_SCHEMA,
                W2_CAPACITY_SCHEMA,
                W2_PLACEMENT_SCHEMA,
                W3_FOUNDATION_SCHEMA,
                W3_TAKEOVER_SCHEMA,
                SCHEMA_REVISION,
            )
            or (value["source_schema"], value["source_sha"])
            not in {
                (SOURCE_SCHEMA, SOURCE_SHA),
                *(
                    (LEGACY_SCHEMA, sha)
                    for sha in _F_RELEASE_SHAS | _G_RELEASE_SHAS | _H_RELEASE_SHAS | _I_RELEASE_SHAS
                ),
                *((J_SCHEMA, sha) for sha in _J_RELEASE_SHAS),
                *((W1_SCHEMA, sha) for sha in _W1_RELEASE_SHAS),
                *((W2_ROUTING_SCHEMA, sha) for sha in _W2_ROUTING_RELEASE_SHAS),
                *((W2_CAPACITY_SCHEMA, sha) for sha in _W2_CAPACITY_RELEASE_SHAS),
                *((W2_PLACEMENT_SCHEMA, sha) for sha in _W2_PLACEMENT_RELEASE_SHAS),
                *((W3_FOUNDATION_SCHEMA, sha) for sha in _W3_FOUNDATION_RELEASE_SHAS),
                *((W3_TAKEOVER_SCHEMA, sha) for sha in _W3_TAKEOVER_RELEASE_SHAS),
            }
            or type(value["target_sha"]) is not str
            or _SHA.fullmatch(value["target_sha"]) is None
            or value["target_sha"] == value["source_sha"]
            or (
                value["source_schema"] == LEGACY_SCHEMA
                and value["target_sha"] in _F_RELEASE_SHAS | {SOURCE_SHA}
            )
            or type(value["evidence_sha256"]) is not str
            or _HEX.fullmatch(value["evidence_sha256"]) is None
            or type(value["control_slots"]) is not int
            or value["control_slots"] not in (1, 2)
            or (previous is None) != (predecessor is None)
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
        intent = ImageTransition(
            operation,
            previous,
            source,
            target,
            value["source_schema"],
            value["target_schema"],
            digest,
            value["control_slots"],
            value["source_sha"],
            value["target_sha"],
            predecessor,
        )
        _intent_prefix(intent)
        return intent
    except Exception:
        raise TransitionUnavailable("image_transition_intent_unavailable") from None


def _kind(intent: ImageTransition) -> int:
    if (
        intent.source_schema == SOURCE_SCHEMA
        and intent.source_sha == SOURCE_SHA
        and intent.target_schema == LEGACY_SCHEMA
    ):
        # Historical receipts retain the original hash algorithm and release pair.
        if intent.target_sha in _F_RELEASE_SHAS:
            return 0
    elif intent.source_schema == LEGACY_SCHEMA and intent.target_schema == LEGACY_SCHEMA:
        if intent.source_sha in _F_RELEASE_SHAS and intent.target_sha in _G_RELEASE_SHAS:
            return 1
        if (
            intent.source_sha in _G_RELEASE_SHAS
            and intent.target_sha not in _F_RELEASE_SHAS | _G_RELEASE_SHAS | {SOURCE_SHA}
        ):
            return 2
        if (
            intent.source_sha in _H_RELEASE_SHAS
            and intent.target_sha
            not in _F_RELEASE_SHAS | _G_RELEASE_SHAS | _H_RELEASE_SHAS | {SOURCE_SHA}
        ):
            return 3
    elif (
        intent.source_schema == LEGACY_SCHEMA
        and intent.source_sha in _I_RELEASE_SHAS
        and intent.target_schema == J_SCHEMA
        and intent.target_sha
        not in _F_RELEASE_SHAS | _G_RELEASE_SHAS | _H_RELEASE_SHAS | _I_RELEASE_SHAS | {SOURCE_SHA}
    ):
        return 4
    elif (
        intent.source_schema == J_SCHEMA
        and intent.source_sha in _J_RELEASE_SHAS
        and intent.target_schema == W1_SCHEMA
        and intent.target_sha
        not in _F_RELEASE_SHAS
        | _G_RELEASE_SHAS
        | _H_RELEASE_SHAS
        | _I_RELEASE_SHAS
        | _J_RELEASE_SHAS
        | {SOURCE_SHA}
    ):
        return 5
    elif (
        intent.source_schema == W1_SCHEMA
        and intent.source_sha in _W1_RELEASE_SHAS
        and intent.target_schema == W2_ROUTING_SCHEMA
        and intent.target_sha
        not in _F_RELEASE_SHAS
        | _G_RELEASE_SHAS
        | _H_RELEASE_SHAS
        | _I_RELEASE_SHAS
        | _J_RELEASE_SHAS
        | _W1_RELEASE_SHAS
        | {SOURCE_SHA}
    ):
        return 6
    elif (
        intent.source_schema == W2_ROUTING_SCHEMA
        and intent.source_sha in _W2_ROUTING_RELEASE_SHAS
        and intent.target_schema == W2_CAPACITY_SCHEMA
        and intent.target_sha
        not in _F_RELEASE_SHAS
        | _G_RELEASE_SHAS
        | _H_RELEASE_SHAS
        | _I_RELEASE_SHAS
        | _J_RELEASE_SHAS
        | _W1_RELEASE_SHAS
        | _W2_ROUTING_RELEASE_SHAS
        | {SOURCE_SHA}
    ):
        return 7
    elif (
        intent.source_schema == W2_CAPACITY_SCHEMA
        and intent.source_sha in _W2_CAPACITY_RELEASE_SHAS
        and intent.target_schema == W2_PLACEMENT_SCHEMA
        and intent.target_sha
        not in _F_RELEASE_SHAS
        | _G_RELEASE_SHAS
        | _H_RELEASE_SHAS
        | _I_RELEASE_SHAS
        | _J_RELEASE_SHAS
        | _W1_RELEASE_SHAS
        | _W2_ROUTING_RELEASE_SHAS
        | _W2_CAPACITY_RELEASE_SHAS
        | {SOURCE_SHA}
    ):
        return 8
    elif (
        intent.source_schema == W2_PLACEMENT_SCHEMA
        and intent.source_sha in _W2_PLACEMENT_RELEASE_SHAS
        and intent.target_schema == W3_FOUNDATION_SCHEMA
        and intent.target_sha
        not in _F_RELEASE_SHAS
        | _G_RELEASE_SHAS
        | _H_RELEASE_SHAS
        | _I_RELEASE_SHAS
        | _J_RELEASE_SHAS
        | _W1_RELEASE_SHAS
        | _W2_ROUTING_RELEASE_SHAS
        | _W2_CAPACITY_RELEASE_SHAS
        | _W2_PLACEMENT_RELEASE_SHAS
        | {SOURCE_SHA}
    ):
        return 9
    elif (
        intent.source_schema == W3_FOUNDATION_SCHEMA
        and intent.source_sha in _W3_FOUNDATION_RELEASE_SHAS
        and intent.target_schema == W3_TAKEOVER_SCHEMA
        and intent.target_sha
        not in _F_RELEASE_SHAS
        | _G_RELEASE_SHAS
        | _H_RELEASE_SHAS
        | _I_RELEASE_SHAS
        | _J_RELEASE_SHAS
        | _W1_RELEASE_SHAS
        | _W2_ROUTING_RELEASE_SHAS
        | _W2_CAPACITY_RELEASE_SHAS
        | _W2_PLACEMENT_RELEASE_SHAS
        | _W3_FOUNDATION_RELEASE_SHAS
        | {SOURCE_SHA}
    ):
        return 10
    elif (
        intent.source_schema == W3_TAKEOVER_SCHEMA
        and intent.source_sha in _W3_TAKEOVER_RELEASE_SHAS
        and intent.target_schema == SCHEMA_REVISION
        and intent.target_sha
        not in _F_RELEASE_SHAS
        | _G_RELEASE_SHAS
        | _H_RELEASE_SHAS
        | _I_RELEASE_SHAS
        | _J_RELEASE_SHAS
        | _W1_RELEASE_SHAS
        | _W2_ROUTING_RELEASE_SHAS
        | _W2_CAPACITY_RELEASE_SHAS
        | _W2_PLACEMENT_RELEASE_SHAS
        | _W3_FOUNDATION_RELEASE_SHAS
        | {SOURCE_SHA}
    ):
        return 11
    raise TransitionUnavailable("image_transition_pair_unavailable")


def _intent_prefix(intent: ImageTransition) -> list[ImageTransition]:
    """Closed release chain retaining each predecessor's original schema and hash."""
    reverse: list[ImageTransition] = []
    current: ImageTransition | None = intent
    while current is not None and len(reverse) <= _MAX_TRANSITIONS:
        reverse.append(current)
        current = current.predecessor
    if current is not None or len(reverse) > _MAX_TRANSITIONS:
        raise TransitionUnavailable("image_transition_conflict")
    prefix = list(reversed(reverse))
    ids = set()
    digests = {prefix[0].source.authority.image_digest}
    for index, item in enumerate(prefix):
        kind = _kind(item)
        if (
            item.operation_id in ids
            or item.operation_id.int == 0
            or item.target.authority.image_digest in digests
            or (index == 0 and item.previous_id is not None)
        ):
            raise TransitionUnavailable("image_transition_conflict")
        if index:
            previous = prefix[index - 1]
            # Original F/G contracts admitted verified equivalent release SHAs.
            # _kind checks that same closed release group; declarations/digests
            # still match exactly, and no original intent is rewritten.
            if (
                kind != _kind(previous) + 1
                or previous.target_sha
                not in {
                    1: _F_RELEASE_SHAS,
                    2: _G_RELEASE_SHAS,
                    3: _H_RELEASE_SHAS,
                    4: _I_RELEASE_SHAS,
                    5: _J_RELEASE_SHAS,
                    6: _W1_RELEASE_SHAS,
                    7: _W2_ROUTING_RELEASE_SHAS,
                    8: _W2_CAPACITY_RELEASE_SHAS,
                    9: _W2_PLACEMENT_RELEASE_SHAS,
                    10: _W3_FOUNDATION_RELEASE_SHAS,
                    11: _W3_TAKEOVER_RELEASE_SHAS,
                }.get(kind, frozenset())
                or item.previous_id != previous.operation_id
                or previous.target_schema != item.source_schema
                or previous.target != item.source
                or previous.control_slots != item.control_slots
            ):
                raise TransitionUnavailable("image_transition_conflict")
        ids.add(item.operation_id)
        digests.add(item.target.authority.image_digest)
    return prefix


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


def _clean(session: Session, *, allow_completed_v3: bool = False) -> None:
    require_dedicated_deployment(session, readonly=True)
    clean = session.scalar(
        text("""
        SELECT NOT EXISTS (SELECT 1 FROM adapter_schedules WHERE enabled)
          AND NOT EXISTS (SELECT 1 FROM workers WHERE scheduling_state <> 'disabled'
            OR (instance_id IS NOT NULL AND instance_lease_until >= clock_timestamp()))
          AND NOT EXISTS (SELECT 1 FROM control_process_instances
                          WHERE lease_until >= clock_timestamp())
          AND NOT EXISTS (SELECT 1 FROM subscription_leases
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
                          WHERE status IN ('claimed','running')
                            OR (cleanup_settled_at IS NULL AND NOT (
                                :allow_completed_v3 AND worker_identity_id IS NULL
                                AND cleanup_summary->>'workspace_cleanup_status'
                                    IS NOT DISTINCT FROM 'completed'
                            ))
                            OR recovery_not_before >= clock_timestamp()
                            OR lease_expires_at >= clock_timestamp())
          AND NOT EXISTS (SELECT 1 FROM adapter_execution_slots WHERE active_attempt_id IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM execution_input_artifact_leases)
          AND NOT EXISTS (SELECT 1 FROM execution_outbox
                          WHERE status NOT IN ('published', 'superseded')
                             OR lease_owner IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM execution_infrastructure_incidents WHERE status = 'open')
          AND NOT EXISTS (SELECT 1 FROM worker_cleanup_intents_v4
                          WHERE completion_id IS NULL OR proof_before >= clock_timestamp())
          AND NOT EXISTS (SELECT 1 FROM attempt_result_intents_v4
                          WHERE finalization_id IS NULL OR proof_before >= clock_timestamp())
          AND NOT EXISTS (SELECT 1 FROM worker_cleanup_requests WHERE status <> 'completed')
    """),
        {"allow_completed_v3": allow_completed_v3},
    )
    if clean is not True:
        raise TransitionUnavailable("image_transition_unsettled")


def _revision(session: Session) -> str:
    versions = session.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(versions) != 1 or versions[0] not in (
        SOURCE_SCHEMA,
        LEGACY_SCHEMA,
        J_SCHEMA,
        W1_SCHEMA,
        W2_ROUTING_SCHEMA,
        W2_CAPACITY_SCHEMA,
        W2_PLACEMENT_SCHEMA,
        W3_FOUNDATION_SCHEMA,
        W3_TAKEOVER_SCHEMA,
        SCHEMA_REVISION,
    ):
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


def _receipt_ids(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("operation_id", "previous_id", "cluster_id", "authority_epoch"):
        if row[key] is not None:
            row[key] = UUID(str(row[key]))
    return row


def _chain(session: Session, *, lock: bool = False) -> list[Any]:
    # Lock in UUID order; reconstruct causality separately. One excess row fails closed.
    return list(
        session.execute(
            text(
                "SELECT * FROM control_image_transitions ORDER BY operation_id "
                f"LIMIT {_MAX_TRANSITIONS + 1}" + (" FOR SHARE" if lock else "")
            )
        ).mappings()
    )


def _validate_chain(rows: list[Any], intent: ImageTransition) -> Any | None:
    prefix = _intent_prefix(intent)
    if len(rows) > _MAX_TRANSITIONS:
        raise TransitionUnavailable("image_transition_conflict")
    if not rows:
        if intent.previous_id is not None:
            raise TransitionUnavailable("image_transition_conflict")
        return None
    indexed = {r["operation_id"]: r for r in rows}
    roots = [r for r in rows if r["previous_id"] is None]
    if len(indexed) != len(rows) or len(roots) != 1:
        raise TransitionUnavailable("image_transition_conflict")
    root = roots[0]
    if root["operation_id"] != prefix[0].operation_id or not _receipt_matches(root, prefix[0]):
        raise TransitionUnavailable("image_transition_conflict")
    ordered = [root]
    digests = {root["source_digest"]}
    while True:
        row = ordered[-1]
        index = _kind(prefix[0]) + len(ordered) - 1
        expected_schema = {
            4: J_SCHEMA,
            5: W1_SCHEMA,
            6: W2_ROUTING_SCHEMA,
            7: W2_CAPACITY_SCHEMA,
            8: W2_PLACEMENT_SCHEMA,
            9: W3_FOUNDATION_SCHEMA,
            10: W3_TAKEOVER_SCHEMA,
            11: SCHEMA_REVISION,
        }.get(index, LEGACY_SCHEMA)
        if (
            row["target_schema"] != expected_schema
            or row["writer_xid"] <= 0
            or row["target_digest"] in digests
        ):
            raise TransitionUnavailable("image_transition_conflict")
        digests.add(row["target_digest"])
        children = [r for r in rows if r["previous_id"] == row["operation_id"]]
        if not children:
            break
        if len(children) != 1 or len(ordered) >= _MAX_TRANSITIONS:
            raise TransitionUnavailable("image_transition_conflict")
        child = children[0]
        if (
            child["source_digest"] != row["target_digest"]
            or child["source_schema"] != row["target_schema"]
            or child["cluster_id"] != root["cluster_id"]
            or child["authority_epoch"] != root["authority_epoch"]
        ):
            raise TransitionUnavailable("image_transition_conflict")
        ordered.append(child)
    # Only known descendants of a historical EF/FG receipt can be observed.
    if len(ordered) != len(rows) or len(rows) > _MAX_TRANSITIONS - _kind(prefix[0]):
        raise TransitionUnavailable("image_transition_conflict")
    for index, item in enumerate(prefix):
        if index < len(ordered):
            if ordered[index]["operation_id"] != item.operation_id or not _receipt_matches(
                ordered[index], item
            ):
                raise TransitionUnavailable("image_transition_conflict")
        elif index != len(prefix) - 1 or item.previous_id != ordered[-1]["operation_id"]:
            raise TransitionUnavailable("image_transition_conflict")
    return ordered[-1]


def _new_source(session: Session, intent: ImageTransition, *, lock: bool = False) -> None:
    if _kind(intent) not in _WRITABLE_TRANSITIONS:
        raise TransitionUnavailable("image_transition_pair_unavailable")
    _intent_prefix(intent)
    latest = _validate_chain(_chain(session, lock=lock), intent)
    if intent.previous_id is None:
        if latest is not None:
            raise TransitionUnavailable("image_transition_conflict")
    elif latest is None or latest["operation_id"] != intent.previous_id:
        raise TransitionUnavailable("image_transition_conflict")


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
               (SELECT coalesce(json_agg(row_to_json(b)), '[]'::json)
                FROM (SELECT * FROM control_image_transitions
                      ORDER BY operation_id LIMIT :chain_limit) b) AS chain,
               latest.target_digest AS latest_digest, latest.target_schema AS latest_schema
        FROM (SELECT 1) seed
        LEFT JOIN control_image_transitions r ON r.operation_id = :operation
        LEFT JOIN control_cluster_authority a ON a.singleton = 1
        LEFT JOIN control_image_transitions latest
          ON NOT EXISTS (SELECT 1 FROM control_image_transitions n
                         WHERE n.previous_id = latest.operation_id)
    """),
            {"operation": intent.operation_id, "chain_limit": _MAX_TRANSITIONS + 1},
        )
        .mappings()
        .one()
    )
    if row["schema_revision"] != revision:
        raise TransitionUnavailable("image_transition_schema_unavailable")
    rows = [_receipt_ids(dict(item)) for item in row["chain"]]
    latest = _validate_chain(rows, intent)
    if (latest["operation_id"] if latest is not None else None) != row["latest_id"]:
        raise TransitionUnavailable("image_transition_conflict")
    receipt = row["receipt"]
    if receipt is None:
        legal = _authority_matches(row, intent.source, intent.source_schema) and (
            row["latest_id"] is None or row["latest_id"] == intent.previous_id
        )
        if not legal:
            raise TransitionUnavailable("image_transition_conflict")
        return {"status": "not_found", "operation_id": str(intent.operation_id)}
    receipt = _receipt_ids(receipt)
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
    if row is None or row["schema_revision"] != revision:
        raise TransitionUnavailable("image_transition_schema_unavailable")
    if revision in (
        LEGACY_SCHEMA,
        J_SCHEMA,
        W1_SCHEMA,
        W2_ROUTING_SCHEMA,
        W2_CAPACITY_SCHEMA,
        W2_PLACEMENT_SCHEMA,
        W3_FOUNDATION_SCHEMA,
        W3_TAKEOVER_SCHEMA,
        SCHEMA_REVISION,
    ):
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
            latest = _validate_chain(_chain(session), intent)
            if (
                latest is None
                or row is None
                or not _receipt_matches(receipt, intent)
                or not _identity_matches(row, intent.target)
                or row["image_digest"] != latest["target_digest"]
                or row["schema_revision"] != latest["target_schema"]
            ):
                raise TransitionUnavailable("image_transition_conflict")
            return (
                "committed_current"
                if latest["operation_id"] == intent.operation_id
                else "committed_superseded"
            )
        _new_source(session, intent)
    elif session.scalar(text("SELECT to_regclass('control_image_transitions')")) is not None:
        raise TransitionUnavailable("image_transition_schema_unavailable")
    if not _authority_matches(row, intent.source, intent.source_schema):
        raise TransitionUnavailable("image_transition_authority_unavailable")
    _clean(
        session,
        allow_completed_v3=intent.target_schema
        in (W2_PLACEMENT_SCHEMA, W3_FOUNDATION_SCHEMA, W3_TAKEOVER_SCHEMA, SCHEMA_REVISION),
    )
    return "source_checked"


def apply(session: Session, intent: ImageTransition, config: Settings) -> None:
    """Stage one top-level transaction. Caller commits, closes, then observes."""
    if _kind(intent) not in _WRITABLE_TRANSITIONS:
        raise TransitionUnavailable("image_transition_pair_unavailable")
    if session.in_transaction() or config.worker_v4_control_slots != intent.control_slots:
        raise TransitionUnavailable("image_transition_transaction_unavailable")
    _limits(session, readonly=False)
    acquire_migration_gate(session)
    revision = _revision(session)
    if revision not in (intent.source_schema, intent.target_schema):
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
        latest = _validate_chain(_chain(session, lock=True), intent)
        if (
            latest is None
            or row is None
            or not _receipt_matches(receipt, intent)
            or not _identity_matches(row, intent.target)
            or row["image_digest"] != latest["target_digest"]
            or row["schema_revision"] != latest["target_schema"]
        ):
            raise TransitionUnavailable("image_transition_conflict")
        # Retried committed operations perform no fencing, even after restart.
        return
    _owner_fence(session, config)
    _new_source(session, intent, lock=True)
    if not _authority_matches(row, intent.source, intent.source_schema):
        raise TransitionUnavailable("image_transition_authority_unavailable")
    session.execute(
        text("SELECT instance_id FROM control_process_instances ORDER BY instance_id FOR UPDATE")
    ).all()
    _clean(
        session,
        allow_completed_v3=intent.target_schema
        in (W2_PLACEMENT_SCHEMA, W3_FOUNDATION_SCHEMA, W3_TAKEOVER_SCHEMA, SCHEMA_REVISION),
    )
    _owner_fence(session, config)
    if revision != intent.source_schema:
        raise TransitionUnavailable("image_transition_schema_unavailable")
    _migrate(session, intent.target_schema)
    if intent.target_schema in (
        W1_SCHEMA,
        W2_ROUTING_SCHEMA,
        W2_CAPACITY_SCHEMA,
        W2_PLACEMENT_SCHEMA,
        W3_FOUNDATION_SCHEMA,
        W3_TAKEOVER_SCHEMA,
        SCHEMA_REVISION,
    ):
        runtime_role = config.worker_v4_database_role
        if runtime_role is None:
            raise TransitionUnavailable("image_transition_owner_unavailable")
        quoted_role = session.get_bind().dialect.identifier_preparer.quote(runtime_role)
    if intent.target_schema == W1_SCHEMA:
        session.execute(text(f"GRANT SELECT ON control_proof_rotations TO {quoted_role}"))
        # Row locks require UPDATE privilege on at least one column. The
        # owner-only trigger prevents even a no-op update by this role.
        session.execute(text(f"GRANT SELECT, UPDATE (id) ON worker_legacy_window TO {quoted_role}"))
        session.execute(
            text(f"GRANT SELECT, INSERT ON worker_adapter_storage_owners_v4 TO {quoted_role}")
        )
    if intent.target_schema == W2_ROUTING_SCHEMA:
        grants = (
            ("SELECT, INSERT", "worker_environment_classes, worker_pool_audit"),
            ("SELECT, INSERT, UPDATE", "worker_pools, worker_pool_members"),
            ("SELECT, INSERT, DELETE", "worker_pool_revisions"),
        )
        for rights, tables in grants:
            session.execute(text(f"REVOKE ALL ON {tables} FROM {quoted_role}"))
            session.execute(text(f"GRANT {rights} ON {tables} TO {quoted_role}"))
        session.execute(
            text(
                "GRANT USAGE ON SEQUENCE worker_pools_id_seq, "
                f"worker_pool_audit_id_seq TO {quoted_role}"
            )
        )
    if intent.target_schema == W2_CAPACITY_SCHEMA:
        tables = "worker_capacities, worker_resource_reservations"
        session.execute(text(f"REVOKE ALL ON {tables} FROM {quoted_role}"))
        session.execute(text(f"GRANT SELECT, INSERT, UPDATE ON {tables} TO {quoted_role}"))
    if intent.target_schema == W2_PLACEMENT_SCHEMA:
        tables = "worker_placement_cutover, worker_capacity_verifiers"
        session.execute(text(f"REVOKE ALL ON {tables} FROM {quoted_role}"))
        session.execute(text(f"GRANT SELECT ON {tables} TO {quoted_role}"))
        session.execute(text(f"REVOKE ALL ON execution_placement_actions FROM {quoted_role}"))
        session.execute(
            text(f"GRANT SELECT, INSERT ON execution_placement_actions TO {quoted_role}")
        )
    if intent.target_schema == W3_FOUNDATION_SCHEMA:
        session.execute(text(f"REVOKE ALL ON control_ha_capacity FROM {quoted_role}"))
        session.execute(text(f"GRANT SELECT ON control_ha_capacity TO {quoted_role}"))
        session.execute(
            text(
                "GRANT UPDATE (observed_at, recorded_at, probe_started_at, writable_free_bytes, "
                "state, blocked) "
                f"ON control_ha_capacity TO {quoted_role}"
            )
        )
    if intent.target_schema == SCHEMA_REVISION:
        session.execute(text(f"REVOKE ALL ON control_storage_bindings FROM {quoted_role}"))
        session.execute(text(f"GRANT SELECT ON control_storage_bindings TO {quoted_role}"))
        session.execute(
            text(f"GRANT SELECT, UPDATE ON control_storage_maintenance TO {quoted_role}")
        )
        session.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON "
                "control_attachment_scopes,control_attachment_objects,control_attachment_jobs "
                f"TO {quoted_role}"
            )
        )
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


def _migrate(session: Session, revision: str) -> None:
    """Join this already fenced owner transaction; Alembic must not commit it."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    migration = Config()
    migration.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[4] / "alembic")
    )
    migration.attributes["connection"] = session.connection()
    command.upgrade(migration, revision)
    if _revision(session) != revision:
        raise TransitionUnavailable("image_transition_schema_unavailable")
