"""Strict local deployment declaration; external claims remain self-reported."""

from __future__ import annotations

import hmac
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from dlr.common.config import Settings
from dlr.control.services.control_cluster_identity import ControlDeclaration, _validated
from dlr.control.services.worker_lease_proof_v4 import public_key_bytes

_FIELDS = {
    "cluster_id",
    "authority_epoch",
    "image_digest",
    "managed_fingerprint",
    "builtin_fingerprint",
    "attachment_fingerprint",
    "capacity_fingerprint",
}


class DeclarationUnavailable(Exception):
    """Generic configuration failure; no path, values or secrets included."""


@dataclass(frozen=True)
class DeploymentDeclaration:
    authority: ControlDeclaration = field(repr=False)
    authority_epoch: UUID
    worker_proof_signing_key: bytes | None = field(default=None, repr=False)


def _read_signing_key(path: str) -> bytes:
    """Read one operator-provisioned seed without following a substituted file."""
    target = Path(path)
    if (
        not path
        or "\x00" in path
        or not target.is_absolute()
        or target != Path(os.path.abspath(target))
        or target.resolve(strict=True) != target
    ):
        raise ValueError
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_size not in {64, 65}
        ):
            raise ValueError
        raw = os.read(fd, 66)
        after = os.fstat(fd)
        visible = target.lstat()
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (visible.st_dev, visible.st_ino) != (after.st_dev, after.st_ino)
            or target.resolve(strict=True) != target
        ):
            raise ValueError
        text = raw.removesuffix(b"\n")
        if re.fullmatch(rb"[0-9a-f]{64}", text) is None:
            raise ValueError
        return bytes.fromhex(text.decode("ascii"))
    finally:
        os.close(fd)


def read_private_json(path: str) -> Any:
    """Read bounded owner-only JSON without following or accepting file races."""
    try:
        if not isinstance(path, str) or not Path(path).is_absolute() or not path or "\x00" in path:
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or not 0 < info.st_size <= 16_384
            ):
                raise ValueError
            data = bytearray()
            while len(data) <= 16_384:
                part = os.read(fd, min(4096, 16_385 - len(data)))
                if not part:
                    break
                data.extend(part)
            after = os.fstat(fd)
            visible = Path(path).lstat()

            def fingerprint(value: os.stat_result) -> tuple[int, ...]:
                return (
                    value.st_dev,
                    value.st_ino,
                    value.st_mode,
                    value.st_uid,
                    value.st_gid,
                    value.st_nlink,
                    value.st_size,
                    value.st_mtime_ns,
                    value.st_ctime_ns,
                )

            if (
                len(data) != info.st_size
                or fingerprint(info) != fingerprint(after)
                or fingerprint(after) != fingerprint(visible)
            ):
                raise ValueError
            raw = bytes(data)
        finally:
            os.close(fd)

        def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError
                value[key] = item
            return value

        return json.loads(raw, object_pairs_hook=unique)
    except Exception:
        raise DeclarationUnavailable("control declaration unavailable") from None


def read_declaration(config: Settings) -> DeploymentDeclaration:
    try:
        path = config.control_identity_file
        if not isinstance(path, str) or not Path(path).is_absolute() or not path or "\x00" in path:
            raise ValueError
        value = read_private_json(path)
        if type(value) is not dict or set(value) not in (
            _FIELDS,
            _FIELDS | {"worker_proof_public_key"},
        ):
            raise ValueError
        cluster_id, epoch = UUID(value["cluster_id"]), UUID(value["authority_epoch"])
        if str(cluster_id) != value["cluster_id"] or str(epoch) != value["authority_epoch"]:
            raise ValueError
        if (
            not isinstance(config.master_key, str)
            or not isinstance(config.admin_token, str)
            or not isinstance(config.worker_token, str)
        ):
            raise ValueError
        public_text = value.get("worker_proof_public_key")
        key_path = config.worker_proof_signing_key_file
        if "worker_proof_public_key" not in value and key_path is None:
            public_key = signing_key = None
        else:
            if (
                type(public_text) is not str
                or re.fullmatch(r"[0-9a-f]{64}", public_text) is None
                or type(key_path) is not str
            ):
                raise ValueError
            public_key = bytes.fromhex(public_text)
            signing_key = _read_signing_key(key_path)
            if (
                cluster_id.int == 0
                or epoch.int == 0
                or not hmac.compare_digest(public_key, public_key_bytes(signing_key))
            ):
                raise ValueError
        authority = ControlDeclaration(
            cluster_id=cluster_id,
            image_digest=value["image_digest"],
            managed_fingerprint=value["managed_fingerprint"],
            builtin_fingerprint=value["builtin_fingerprint"],
            attachment_fingerprint=value["attachment_fingerprint"],
            capacity_fingerprint=value["capacity_fingerprint"],
            master_key=config.master_key,
            admin_token=config.admin_token,
            worker_token=config.worker_token,
            worker_proof_public_key=public_key,
        )
        _validated(authority)
        return DeploymentDeclaration(authority, epoch, signing_key)
    except Exception:
        raise DeclarationUnavailable("control declaration unavailable") from None
