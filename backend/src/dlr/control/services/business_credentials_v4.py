"""Frozen deployment permission and bounded metadata for business credentials.

Only ``resolve_values`` decrypts, inside the supervised Control Claim child.
The policy contains references only and is never a Worker-provided permission.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Text, case, cast, func, select
from sqlalchemy.orm import Session

from dlr.common import business_credentials_v4 as contract
from dlr.common import config
from dlr.control.models import (
    AdapterCredentialBinding,
    Execution,
    ExecutionCredentialBindingSnapshot,
    Worker,
)
from dlr.control.models.platform import Credential

_MAX_POLICY_BYTES = 16384


@dataclass(frozen=True)
class Grant:
    worker_id: int
    worker_identity_id: UUID
    credential_ids: tuple[int, ...]


@dataclass(frozen=True)
class Policy:
    grants: tuple[Grant, ...] = field(default=(), repr=False)
    owner_pid: int = field(default_factory=os.getpid, repr=False)

    def document(self) -> dict[str, Any]:
        return {
            "version": 1,
            "workers": [
                {
                    "worker_id": grant.worker_id,
                    "worker_identity_id": str(grant.worker_identity_id),
                    "credential_ids": list(grant.credential_ids),
                }
                for grant in self.grants
            ],
        }

    def snapshot(self) -> dict[str, Any]:
        document = self.document()
        return {"policy": document, "digest": _digest(document)}

    def permits(self, worker: Worker, refs: tuple[contract.Binding, ...]) -> bool:
        if self.owner_pid != os.getpid():
            return False
        needed = {ref.credential_id for ref in refs}
        return any(
            grant.worker_id == worker.id
            and grant.worker_identity_id == worker.identity_id
            and needed.issubset(grant.credential_ids)
            for grant in self.grants
        )


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _parse(value: Any) -> Policy:
    try:
        if (
            type(value) is not dict
            or set(value) != {"version", "workers"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or type(value["workers"]) is not list
            or len(value["workers"]) > 2
        ):
            raise ValueError
        grants = []
        ids: set[int] = set()
        identities: set[UUID] = set()
        for item in value["workers"]:
            if type(item) is not dict or set(item) != {
                "worker_id",
                "worker_identity_id",
                "credential_ids",
            }:
                raise ValueError
            identity = UUID(item["worker_identity_id"])
            credentials = item["credential_ids"]
            if (
                not contract.positive_id(item["worker_id"])
                or str(identity) != item["worker_identity_id"]
                or identity.int == 0
                or item["worker_id"] in ids
                or identity in identities
                or type(credentials) is not list
                or len(credentials) > contract.MAX_BINDINGS
                or any(not contract.positive_id(n) for n in credentials)
                or len(set(credentials)) != len(credentials)
            ):
                raise ValueError
            ids.add(item["worker_id"])
            identities.add(identity)
            grants.append(Grant(item["worker_id"], identity, tuple(sorted(credentials))))
        return Policy(tuple(sorted(grants, key=lambda g: g.worker_id)))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise contract.BusinessCredentialError("credentials_policy") from None


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in items:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def frozen(settings: config.Settings | None = None) -> Policy:
    settings = config.settings if settings is None else settings
    policy = settings._business_credential_policy
    if policy is None and settings.worker_business_credential_policy_file is None:
        return Policy()
    if type(policy) is not Policy or policy.owner_pid != os.getpid():
        raise contract.BusinessCredentialError("credentials_unavailable")
    return policy


def freeze(settings: config.Settings) -> None:
    """Install once in a Control process, never re-read a wider live file."""
    if settings._business_credential_policy is not None:
        frozen(settings)
        return
    path = settings.worker_business_credential_policy_file
    if path is None:
        settings._business_credential_policy = Policy()
        return
    fd = -1
    try:
        target = Path(path)
        if not target.is_absolute() or target != target.resolve(strict=True):
            raise ValueError
        fd = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or not 0 < before.st_size <= _MAX_POLICY_BYTES
        ):
            raise ValueError
        raw = os.read(fd, _MAX_POLICY_BYTES + 1)
        after, visible = os.fstat(fd), target.lstat()
        if (
            len(raw) != before.st_size
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (visible.st_dev, visible.st_ino) != (before.st_dev, before.st_ino)
            or target != target.resolve(strict=True)
        ):
            raise ValueError
        settings._business_credential_policy = _parse(json.loads(raw, object_pairs_hook=_pairs))
    except (OSError, ValueError, TypeError, RecursionError):
        raise contract.BusinessCredentialError("credentials_policy") from None
    finally:
        if fd >= 0:
            os.close(fd)


def install_snapshot(value: Any, settings: config.Settings) -> None:
    """Only the private parent IPC calls this, before any Claim payload read."""
    try:
        if type(value) is not dict or set(value) != {"policy", "digest"}:
            raise ValueError
        if len(json.dumps(value).encode()) > _MAX_POLICY_BYTES:
            raise ValueError
        policy = _parse(value["policy"])
        digest = value["digest"]
        if (
            type(digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or _digest(policy.document()) != digest
        ):
            raise ValueError
        if settings._business_credential_policy is not None:
            raise ValueError
        settings._business_credential_policy = policy
    except (ValueError, TypeError, RecursionError):
        raise contract.BusinessCredentialError("credentials_policy") from None


def adapter_bindings(session: Session, adapter_id: int) -> tuple[contract.Binding, ...]:
    rows = session.execute(
        select(
            AdapterCredentialBinding.id,
            AdapterCredentialBinding.credential_id,
            AdapterCredentialBinding.env_key,
            AdapterCredentialBinding.field,
        )
        .where(AdapterCredentialBinding.adapter_id == adapter_id)
        .order_by(AdapterCredentialBinding.env_key, AdapterCredentialBinding.id)
        .limit(contract.MAX_BINDINGS + 1)
    ).all()
    return contract.bindings(
        [
            {
                "binding_id": row.id,
                "credential_id": row.credential_id,
                "env_key": row.env_key,
                "field": row.field,
            }
            for row in rows
        ]
    )


def execution_bindings(session: Session, execution_id: int) -> tuple[contract.Binding, ...]:
    snapshot = Execution.credential_bindings_snapshot
    kind, count, size = session.execute(
        select(
            func.jsonb_typeof(snapshot),
            case(
                (func.jsonb_typeof(snapshot) == "array", func.jsonb_array_length(snapshot)),
                else_=None,
            ),
            func.octet_length(cast(snapshot, Text)),
        ).where(Execution.id == execution_id)
    ).one()
    if (
        kind != "array"
        or count is None
        or count > contract.MAX_BINDINGS
        or size > contract.MAX_BINDING_METADATA_BYTES
    ):
        raise contract.BusinessCredentialError("credentials_budget")
    value = session.scalar(select(snapshot).where(Execution.id == execution_id))
    refs = contract.bindings(value)
    rows = session.execute(
        select(
            ExecutionCredentialBindingSnapshot.binding_id,
            ExecutionCredentialBindingSnapshot.credential_id,
            ExecutionCredentialBindingSnapshot.env_key,
            ExecutionCredentialBindingSnapshot.field,
        )
        .where(ExecutionCredentialBindingSnapshot.execution_id == execution_id)
        .order_by(ExecutionCredentialBindingSnapshot.id)
        .limit(contract.MAX_BINDINGS + 1)
    ).all()
    if tuple(contract.Binding(*row) for row in rows) != refs:
        raise contract.BusinessCredentialError()
    return refs


def check(
    session: Session,
    refs: tuple[contract.Binding, ...],
    worker_id: int | None,
    *,
    lock: bool = False,
) -> None:
    if not refs:
        return
    worker = session.get(Worker, worker_id) if worker_id is not None else None
    if (
        worker is None
        or type(worker.isolation_capabilities) is not dict
        or worker.isolation_capabilities.get(contract.CAPABILITY) is not True
    ):
        raise contract.BusinessCredentialError("credentials_mode")
    if not frozen().permits(worker, refs):
        raise contract.BusinessCredentialError("credentials_policy")
    ids = sorted({ref.credential_id for ref in refs})
    query = (
        select(
            Credential.id, Credential.type, func.octet_length(Credential.ciphertext).label("size")
        )
        .where(Credential.id.in_(ids))
        .order_by(Credential.id)
    )
    if lock:
        query = query.with_for_update()
    rows = session.execute(query).all()
    if len(rows) != len(ids):
        raise contract.BusinessCredentialError()
    total = 0
    for row in rows:
        if row.size <= 0 or row.size > contract.MAX_CIPHERTEXT_BYTES:
            raise contract.BusinessCredentialError("credentials_budget")
        total += row.size
        for ref in refs:
            if ref.credential_id == row.id:
                contract.credential_field(row.type, ref.field)
    if total > contract.MAX_CIPHERTEXT_TOTAL_BYTES:
        raise contract.BusinessCredentialError("credentials_budget")


def resolve_values(session: Session, execution: Execution, worker: Worker) -> dict[str, str]:
    from dlr.control.services.secrets import decrypt_fields

    refs = execution_bindings(session, execution.id)
    check(session, refs, worker.id, lock=True)
    result: dict[str, str] = {}
    for credential_id in sorted({ref.credential_id for ref in refs}):
        # Its row is already locked and its exact ciphertext length was checked.
        kind, ciphertext = session.execute(
            select(Credential.type, Credential.ciphertext).where(Credential.id == credential_id)
        ).one()
        try:
            fields = decrypt_fields(ciphertext)
            for ref in refs:
                if ref.credential_id == credential_id:
                    result[ref.env_key] = fields[contract.credential_field(kind, ref.field)]
        except Exception:
            raise contract.BusinessCredentialError("credentials_unavailable") from None
    return contract.values(refs, result)
