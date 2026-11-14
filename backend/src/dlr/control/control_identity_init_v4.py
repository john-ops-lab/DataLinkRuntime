"""Explicit local Control authority initialization; no automatic replacement."""

from __future__ import annotations

import json
import sys

from sqlalchemy import text
from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.common.schema_gate import try_control_gate
from dlr.control import db
from dlr.control.services import control_cluster_identity as identity
from dlr.control.services.control_declaration_v4 import DeploymentDeclaration, read_declaration


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
