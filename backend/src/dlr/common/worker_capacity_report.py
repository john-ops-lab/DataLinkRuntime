"""Canonical, instance-bound observations signed by an approved host verifier."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints

from dlr.common.jcs import canonicalize

_CONTEXT = b"dlr-worker-capacity-observation-v1\x00"
CapacityFingerprint = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
CapacitySignature = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{128}$")]


class Resources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu_millis: StrictInt = Field(ge=0, lt=2**63)
    memory_bytes: StrictInt = Field(ge=0, lt=2**63)
    pids: StrictInt = Field(ge=0, lt=2**63)
    tmp_bytes: StrictInt = Field(ge=0, lt=2**63)
    slots: StrictInt = Field(ge=0, lt=2**63)


class CapacityObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_identity_id: UUID
    instance_id: UUID
    instance_generation: StrictInt = Field(gt=0, lt=2**63)
    sampled_at: datetime
    envelope_fingerprint: CapacityFingerprint
    total: Resources
    headroom: Resources
    nofile: StrictInt = Field(gt=0, lt=2**63)
    storage_ready: StrictBool


class SignedCapacityObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation: CapacityObservation
    signature: CapacitySignature


def _signed_bytes(observation: CapacityObservation) -> bytes:
    # Resource sizes can exceed the exact JSON Number domain. Encode each as
    # a decimal string in the signing transcript, independent of JSON parsing.
    fields = observation.model_dump(mode="json")
    for name in ("total", "headroom"):
        fields[name] = {key: str(value) for key, value in fields[name].items()}
    for name in ("instance_generation", "nofile"):
        fields[name] = str(fields[name])
    return _CONTEXT + canonicalize(fields)


def sign_observation(observation: CapacityObservation, key: bytes) -> SignedCapacityObservation:
    signed = Ed25519PrivateKey.from_private_bytes(key).sign(_signed_bytes(observation))
    return SignedCapacityObservation(observation=observation, signature=signed.hex())


def verify_observation(report: SignedCapacityObservation, key: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(
            bytes.fromhex(report.signature), _signed_bytes(report.observation)
        )
    except (InvalidSignature, ValueError):
        return False
    return True
