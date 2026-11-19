"""Explicit local Control authority initialization; no automatic replacement."""

from __future__ import annotations

import json
import sys
from contextlib import suppress
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from dlr.common.config import settings
from dlr.common.schema_gate import acquire_migration_gate, try_control_gate
from dlr.control import db
from dlr.control.services import control_cluster_identity as identity
from dlr.control.services import control_image_transition_v4 as transition
from dlr.control.services import control_proof_rotation_v4 as proof_rotation
from dlr.control.services import worker_legacy_window
from dlr.control.services.control_declaration_v4 import DeploymentDeclaration, read_declaration


def maintain(
    factory: sessionmaker[Session], command: str, intent: transition.ImageTransition
) -> dict[str, object]:
    """Close the writer before independent readback, including a lost COMMIT reply."""
    if command == "check":
        with factory() as session:
            return {"status": transition.check(session, intent, settings)}
    if command == "apply":
        # A failed statement/COMMIT is never assumed to have rolled back.
        # Close this connection before a separate read-only snapshot resolves it.
        refused = False
        with factory() as session:
            try:
                transition.apply(session, intent, settings)
                session.commit()
            except transition.TransitionUnavailable:
                refused = True
                with suppress(Exception):
                    session.rollback()
            except Exception:
                with suppress(Exception):
                    session.rollback()
        with factory() as session:
            observed = transition.observe(session, intent)
        return {"status": "refused", "observed_status": observed["status"]} if refused else observed
    if command == "observe":
        with factory() as session:
            return transition.observe(session, intent)
    raise ValueError


def _maintenance_main(args: list[str]) -> int:
    try:
        # Only filenames are arguments. Invalid input is never echoed by a parser.
        if not args or args[0] not in {"check", "apply", "observe"}:
            raise ValueError
        flags = dict(zip(args[1::2], args[2::2], strict=True))
        required = {"--source", "--target", "--intent"}
        levels = (
            "previous",
            "root",
            "origin",
            "ancestor",
            "initial",
            "earliest",
            "first",
            "oldest",
        )
        accepted = [
            required | {f"--{name}-{part}" for name in levels[:n] for part in ("source", "intent")}
            for n in range(len(levels) + 1)
        ]
        if len(flags) != (len(args) - 1) // 2 or set(flags) not in accepted:
            raise ValueError
        source = read_declaration(
            settings.model_copy(update={"control_identity_file": flags["--source"]})
        )
        target = read_declaration(
            settings.model_copy(update={"control_identity_file": flags["--target"]})
        )
        sources = [source]
        paths = []
        for name in levels:
            if f"--{name}-intent" not in flags:
                break
            sources.append(
                read_declaration(
                    settings.model_copy(update={"control_identity_file": flags[f"--{name}-source"]})
                )
            )
            paths.append(flags[f"--{name}-intent"])
        predecessor = None
        for index in reversed(range(len(paths))):
            predecessor = transition.read_intent(
                paths[index], sources[index + 1], sources[index], predecessor=predecessor
            )
        intent = transition.read_intent(flags["--intent"], source, target, predecessor=predecessor)
        engine = create_engine(
            settings.database_url,
            poolclass=NullPool,
            connect_args={
                "connect_timeout": 5,
                "options": "-c statement_timeout=10000 -c lock_timeout=5000",
            },
        )
        try:
            result = maintain(sessionmaker(engine), args[0], intent)
        finally:
            engine.dispose()
        print(json.dumps(result))
        return (
            0
            if result["status"] in {"source_checked", "ready_to_apply", "committed_current"}
            else 1
        )
    except Exception:
        print(json.dumps({"status": "unknown"}))
        return 1


def initialize(session: Session, deployment: DeploymentDeclaration) -> str:
    """Prepare an exact first authority or verify an identical existing one.

    The caller must commit; an exception during commit is unknown, not success.
    No old authority or configuration is overwritten.
    """
    try_control_gate(session)
    exists = session.scalar(
        text("SELECT EXISTS (SELECT 1 FROM control_cluster_authority WHERE singleton=1)")
    )
    if exists:
        authority = identity._authority(session, deployment.authority)
        if authority["authority_epoch"] != deployment.authority_epoch:
            raise identity.ControlIdentityUnavailable("control identity unavailable")
        # Fingerprints represent independent declared dependencies; verify all
        # exactly even though they do not establish actual shared storage.
        for key in (
            "managed_fingerprint",
            "builtin_fingerprint",
            "attachment_fingerprint",
            "capacity_fingerprint",
        ):
            if authority[key] != getattr(deployment.authority, key):
                raise identity.ControlIdentityUnavailable("control identity unavailable")
        return "verified"
    identity.provision_authority(
        session, deployment.authority, authority_epoch=deployment.authority_epoch
    )
    return "initialized"


def main() -> int:
    if len(sys.argv) > 1:
        if sys.argv[1] in {"legacy-window-open", "legacy-window-close", "legacy-window-status"}:
            return _legacy_window_main(sys.argv[1:])
        if sys.argv[1] in {"proof-authority-apply", "proof-authority-observe"}:
            return _proof_rotation_main(sys.argv[1:])
        if sys.argv[1] in {
            "placement-pause",
            "placement-reconcile",
            "placement-enable",
            "placement-status",
            "placement-approve-capacity",
        }:
            return _placement_main(sys.argv[1:])
        return _maintenance_main(sys.argv[1:])
    try:
        deployment = read_declaration(settings)
        with db.SessionLocal() as session:
            result = initialize(session, deployment)
            session.commit()
        print(json.dumps({"status": result}))
        return 0
    except Exception:
        # This includes a commit response loss: do not claim initialization.
        print(json.dumps({"status": "unavailable_or_commit_unknown"}))
        return 1


def _placement_main(args: list[str]) -> int:
    """Drive the owner-only handoff on the already verified current schema."""
    from sqlalchemy import select

    from dlr.control.models import Execution
    from dlr.control.services import worker_capacity
    from dlr.control.services import worker_placement_cutover as placement

    def verify_owner(session: Session) -> None:
        try_control_gate(session)
        transition._owner_authorized(session, settings)
        declaration = read_declaration(settings)
        authority = identity._authority(session, declaration.authority)
        if authority["authority_epoch"] != declaration.authority_epoch:
            raise identity.ControlIdentityUnavailable("control identity unavailable")

    try:
        action = args[0]
        if len(args) != (4 if action == "placement-approve-capacity" else 1):
            raise ValueError
        with db.SessionLocal() as session:
            verify_owner(session)
            if action == "placement-approve-capacity":
                worker_id = int(args[1])
                if not 0 < worker_id < 2**63 or len(args[3]) != 64:
                    raise ValueError
                worker_capacity.approve_host_verifier(
                    session, worker_id, args[2], bytes.fromhex(args[3])
                )
                session.commit()
                print(json.dumps({"status": "capacity_verifier_committed"}))
                return 0
            if action == "placement-status":
                print(json.dumps({"status": "observed", "placement_mode": placement.mode(session)}))
                return 0
            if action == "placement-reconcile":
                if placement.mode(session) != "paused":
                    raise ValueError
                candidates = list(
                    session.scalars(
                        select(Execution.id)
                        .where(
                            ~Execution.requires_worker_reservation,
                            Execution.status.in_(("queued", "retry_wait")),
                        )
                        .order_by(Execution.id)
                        .limit(100)
                    )
                )
            else:
                changed = (
                    placement.pause(session)
                    if action == "placement-pause"
                    else placement.activate(session)
                )
                session.commit()
                print(json.dumps({"status": "committed", "placement_mode": changed}))
                return 0
        converted = 0
        for execution_id in candidates:
            with db.SessionLocal() as session:
                verify_owner(session)
                converted += int(placement.convert_waiting(session, execution_id))
                session.commit()
        print(json.dumps({"status": "committed", "converted": converted}))
        return 0
    except HTTPException as error:
        detail = error.detail
        code = detail.get("code") if isinstance(detail, dict) else None
        print(json.dumps({"status": "refused", "code": code}))
        return 1
    except Exception:
        print(json.dumps({"status": "placement_unavailable_or_commit_unknown"}))
        return 1


def _legacy_window_main(args: list[str]) -> int:
    opening = args[0] == "legacy-window-open"
    try:
        if len(args) != (2 if opening else 1) or not worker_legacy_window.enabled_mode():
            raise worker_legacy_window.LegacyWindowUnavailable
        with db.SessionLocal() as session:
            acquire_migration_gate(session)
            transition._owner_fence(session, settings)
            if opening:
                deployment = read_declaration(settings)
                # Verify existing authority if this is a restart of operator
                # preparation; an unexpected declaration fails closed.
                initialize(session, deployment)
                worker_legacy_window.open_once(session, datetime.fromisoformat(args[1]))
                result = "legacy_window_opened"
            elif args[0] == "legacy-window-close":
                worker_legacy_window.close(session)
                result = "legacy_window_closed"
            else:
                # A separate connection is also used after a lost write reply.
                row = (
                    session.execute(
                        text(
                            "SELECT opened_at, deadline_at, closed_at "
                            "FROM worker_legacy_window WHERE id=1"
                        )
                    )
                    .mappings()
                    .one()
                )
                print(json.dumps({"status": "observed", **row}, default=str))
                return 0
            transition._owner_fence(session, settings)
            session.commit()
        print(json.dumps({"status": result}))
        return 0
    except Exception:
        print(json.dumps({"status": "legacy_window_unavailable_or_commit_unknown"}))
        return 1


def _proof_rotation_main(args: list[str]) -> int:
    try:
        if len(args) != 11:
            raise ValueError
        flags = dict(zip(args[1::2], args[2::2], strict=True))
        if set(flags) != {"--source", "--source-key", "--target", "--target-key", "--intent"}:
            raise ValueError
        source, target = (
            read_declaration(
                settings.model_copy(
                    update={
                        "control_identity_file": flags[flag],
                        "worker_proof_signing_key_file": flags[f"{flag}-key"],
                    }
                )
            )
            for flag in ("--source", "--target")
        )
        intent = proof_rotation.read_intent(flags["--intent"], source, target)
        engine = create_engine(settings.database_url, poolclass=NullPool)
        try:
            factory = sessionmaker(engine)
            if args[0] == "proof-authority-apply":
                with factory() as session:
                    try:
                        proof_rotation.apply(session, intent, settings)
                        session.commit()
                    except Exception:
                        # The commit may have reached PostgreSQL. Close this
                        # connection before trying to observe the exact intent.
                        with suppress(Exception):
                            session.rollback()
            with factory() as session:
                result = proof_rotation.observe(session, intent)
        finally:
            engine.dispose()
        print(json.dumps(result))
        return 0 if result["status"] == "committed_current" else 1
    except Exception:
        print(json.dumps({"status": "proof_authority_unavailable_or_commit_unknown"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
