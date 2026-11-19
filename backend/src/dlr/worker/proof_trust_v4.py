"""Read an operator-provisioned Worker proof trust anchor.

The file is configuration, never learned from a Control response. Deployment
must protect its directory and fence old instances while changing trust.
"""

import json
import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID


class ProofTrustUnavailable(Exception):
    """Non-sensitive trust configuration failure."""


@dataclass(frozen=True)
class ProofTrust:
    cluster_id: UUID
    authority_epoch: UUID
    public_key: bytes = field(repr=False)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _read_private_object(path: Path) -> dict[str, object]:
    """Read a closed Worker configuration object without opening substituted files."""
    fd = -1
    try:
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or path != Path(os.path.abspath(path))
            or path.resolve(strict=True) != path
        ):
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or not 0 < before.st_size <= 4096
        ):
            raise ValueError
        data = bytearray()
        while len(data) <= 4096:
            chunk = os.read(fd, min(4097 - len(data), 4096))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        visible = path.lstat()
        if (
            len(data) != before.st_size
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (visible.st_dev, visible.st_ino) != (after.st_dev, after.st_ino)
            or path.resolve(strict=True) != path
        ):
            raise ValueError
        value = json.loads(bytes(data), object_pairs_hook=_unique_pairs)
        if type(value) is not dict:
            raise ValueError
        return value
    finally:
        if fd >= 0:
            with suppress(OSError):
                os.close(fd)


def load(path: Path) -> ProofTrust:
    """Load exact identity and public key from a private, stable local file."""
    try:
        value = _read_private_object(path)
        if set(value) != {
            "schema_version",
            "cluster_id",
            "authority_epoch",
            "worker_proof_public_key",
        }:
            raise ValueError
        key = value["worker_proof_public_key"]
        cluster_text, epoch_text = value["cluster_id"], value["authority_epoch"]
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or type(key) is not str
            or re.fullmatch(r"[0-9a-f]{64}", key) is None
            or type(cluster_text) is not str
            or type(epoch_text) is not str
        ):
            raise ValueError
        cluster = UUID(cluster_text)
        epoch = UUID(epoch_text)
        if (
            cluster.int == 0
            or epoch.int == 0
            or str(cluster) != value["cluster_id"]
            or str(epoch) != value["authority_epoch"]
        ):
            raise ValueError
        return ProofTrust(cluster, epoch, bytes.fromhex(key))
    except Exception:
        raise ProofTrustUnavailable("worker proof trust unavailable") from None
