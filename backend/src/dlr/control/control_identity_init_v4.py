"""Explicit local Control authority initialization; no automatic replacement."""

from __future__ import annotations

import json
import sys
from contextlib import suppress

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from dlr.common.config import settings
from dlr.common.schema_gate import try_control_gate
from dlr.control import db
from dlr.control.services import control_cluster_identity as identity
from dlr.control.services import control_image_transition_v4 as transition
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
        if len(args) not in {7, 11, 15} or args[0] not in {"check", "apply", "observe"}:
            raise ValueError
        flags = dict(zip(args[1::2], args[2::2], strict=True))
        required = {"--source", "--target", "--intent"}
        if len(flags) != (len(args) - 1) // 2 or set(flags) not in (
            required,
            required | {"--previous-source", "--previous-intent"},
            required | {"--previous-source", "--previous-intent", "--root-source", "--root-intent"},
        ):
            raise ValueError
        source = read_declaration(
            settings.model_copy(update={"control_identity_file": flags["--source"]})
        )
        target = read_declaration(
            settings.model_copy(update={"control_identity_file": flags["--target"]})
        )
        predecessor = None
        if "--previous-intent" in flags:
            previous_source = read_declaration(
                settings.model_copy(update={"control_identity_file": flags["--previous-source"]})
            )
            root = None
            if "--root-intent" in flags:
                root_source = read_declaration(
                    settings.model_copy(update={"control_identity_file": flags["--root-source"]})
                )
                root = transition.read_intent(flags["--root-intent"], root_source, previous_source)
            predecessor = transition.read_intent(
                flags["--previous-intent"], previous_source, source, predecessor=root
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


if __name__ == "__main__":
    sys.exit(main())
