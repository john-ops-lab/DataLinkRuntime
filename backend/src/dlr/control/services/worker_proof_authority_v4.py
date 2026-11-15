"""Bind an operator-provisioned Worker proof key to the Control authority.

This check does not establish old-binary fencing, physical process isolation,
clock health, or authorization to expose any Worker route. Callers must use the
existing cluster gates as well and must not treat the returned object as a
capability valid after the transaction ends.
"""

import hmac

from sqlalchemy.orm import Session

from dlr.control.services import control_cluster_identity as identity
from dlr.control.services.control_declaration_v4 import DeploymentDeclaration
from dlr.control.services.worker_lease_candidates_v4 import ProofAuthority


def for_transaction(session: Session, deployment: DeploymentDeclaration) -> ProofAuthority:
    """Compare the declared public key and epoch under the authority row lock."""
    try:
        if (
            type(deployment) is not DeploymentDeclaration
            or deployment.worker_proof_signing_key is None
            or deployment.authority.worker_proof_public_key is None
        ):
            raise ValueError
        result = ProofAuthority(
            deployment.authority.cluster_id,
            deployment.authority_epoch,
            deployment.worker_proof_signing_key,
        )
        if not hmac.compare_digest(result.public_key, deployment.authority.worker_proof_public_key):
            raise ValueError
        authority = identity._authority(session, deployment.authority)
        if authority["authority_epoch"] != deployment.authority_epoch:
            raise ValueError
        return result
    except Exception:
        raise identity.ControlIdentityUnavailable("control identity unavailable") from None
