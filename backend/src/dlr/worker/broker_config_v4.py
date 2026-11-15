"""Isolated v4 Broker connection configuration; no consumer is enabled here."""

from __future__ import annotations

import json
import os
import ssl
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import pika

from dlr.common.rabbitmq_names import worker_queue_name
from dlr.worker import enrollment_v4, runtime_credential_v4

_MAX_CONFIG_BYTES = 8192


class BrokerConfigurationError(ValueError):
    """Invalid Broker settings, without reflecting credentials or paths."""


def _valid_text(value: object, *, limit: int, controls: bool) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return False
    return (
        len(encoded) <= limit
        and "\x00" not in value
        and (not controls or not any(ord(char) < 32 or ord(char) == 127 for char in value))
    )


@dataclass(frozen=True)
class BrokerConfigV4:
    """Fixed Worker, separate AMQP credentials, and an explicit TLS trust root."""

    worker_id: int
    host: str = field(repr=False)
    port: int
    vhost: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    ca_file: Path = field(repr=False)

    def __post_init__(self) -> None:
        try:
            worker_queue_name(self.worker_id)
        except ValueError:
            raise BrokerConfigurationError("Broker configuration invalid") from None
        if (
            not _valid_text(self.host, limit=253, controls=True)
            or self.host != self.host.strip()
            or any(char in self.host for char in "/@?#[]%")
            or type(self.port) is not int
            or not 1 <= self.port <= 65535
            or not _valid_text(self.vhost, limit=255, controls=True)
            or not _valid_text(self.username, limit=255, controls=True)
            or self.username == "guest"
            or not _valid_text(self.password, limit=1024, controls=False)
            or not isinstance(self.ca_file, Path)
            or not self.ca_file.is_absolute()
        ):
            raise BrokerConfigurationError("Broker configuration invalid")

    @classmethod
    def from_private_file(
        cls, path: Path, *, worker_id: int, worker_identity_id: UUID
    ) -> BrokerConfigV4:
        """Load one durable, identity-bound AMQP configuration without logging it.

        The pre-existing 0700 directory remains under trusted operator control.
        Re-reading creates fresh connection parameters after rotation; it does
        not close existing Broker connections or enable a consumer.
        """
        directory = source = -1
        try:
            if (
                not isinstance(path, Path)
                or not path.is_absolute()
                or not path.name
                or type(worker_id) is not int
                or not 0 < worker_id < 2**63
                or type(worker_identity_id) is not UUID
                or worker_identity_id.int == 0
                or path.parent.resolve(strict=True) != path.parent
            ):
                raise ValueError("invalid configuration")
            directory = os.open(
                path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            parent = os.fstat(directory)
            parent_identity = (parent.st_dev, parent.st_ino)
            runtime_credential_v4._lock_directory(directory, exclusive=False)
            enrollment_v4._anchor(directory, path.parent, parent_identity)
            source = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
            before = os.fstat(source)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink != 1
                or not 1 <= before.st_size <= _MAX_CONFIG_BYTES
            ):
                raise ValueError("invalid configuration")

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

            def check_file() -> None:
                current = os.fstat(source)
                visible = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if fingerprint(current) != fingerprint(before) or fingerprint(
                    visible
                ) != fingerprint(before):
                    raise ValueError("configuration changed")

            def read_file() -> bytes:
                os.lseek(source, 0, os.SEEK_SET)
                result = bytearray()
                while len(result) <= _MAX_CONFIG_BYTES:
                    part = os.read(source, min(4096, _MAX_CONFIG_BYTES + 1 - len(result)))
                    if not part:
                        break
                    result.extend(part)
                if len(result) != before.st_size:
                    raise ValueError("invalid configuration")
                return bytes(result)

            check_file()
            raw = read_file()
            check_file()
            os.fsync(source)
            os.fsync(directory)
            enrollment_v4._anchor(directory, path.parent, parent_identity)
            check_file()
            if read_file() != raw:
                raise ValueError("configuration changed")
            check_file()
            enrollment_v4._anchor(directory, path.parent, parent_identity)

            def invalid_constant(_value: str) -> None:
                raise ValueError("invalid configuration")

            value = json.loads(
                raw, object_pairs_hook=enrollment_v4._unique_pairs, parse_constant=invalid_constant
            )
            if (
                type(value) is not dict
                or set(value)
                != {
                    "schema_version",
                    "worker_id",
                    "worker_identity_id",
                    "host",
                    "port",
                    "vhost",
                    "username",
                    "password",
                    "ca_file",
                }
                or type(value["schema_version"]) is not int
                or value["schema_version"] != 1
                or type(value["worker_id"]) is not int
                or value["worker_id"] != worker_id
                or value["worker_identity_id"] != str(worker_identity_id)
                or type(value["ca_file"]) is not str
            ):
                raise ValueError("invalid configuration")
            return cls(
                worker_id=worker_id,
                host=value["host"],
                port=value["port"],
                vhost=value["vhost"],
                username=value["username"],
                password=value["password"],
                ca_file=Path(value["ca_file"]),
            )
        except Exception:
            raise BrokerConfigurationError("Broker configuration invalid") from None
        finally:
            if source >= 0:
                with suppress(OSError):
                    os.close(source)
            if directory >= 0:
                with suppress(OSError):
                    os.close(directory)

    @property
    def queue(self) -> str:
        return worker_queue_name(self.worker_id)

    def connection_parameters(self) -> pika.ConnectionParameters:
        """Create fresh Pika parameters; never disable peer or hostname checks."""
        try:
            context = ssl.create_default_context(cafile=str(self.ca_file))
            if context.verify_mode != ssl.CERT_REQUIRED or context.check_hostname is not True:
                raise ValueError("TLS verification unavailable")
            return pika.ConnectionParameters(
                host=self.host,
                port=self.port,
                virtual_host=self.vhost,
                credentials=pika.PlainCredentials(
                    self.username, self.password, erase_on_connect=True
                ),
                ssl_options=pika.SSLOptions(context, self.host),
                heartbeat=30,
                socket_timeout=5,
                stack_timeout=10,
                blocked_connection_timeout=10,
                connection_attempts=1,
                retry_delay=0,
            )
        except (OSError, TypeError, ValueError, ssl.SSLError):
            raise BrokerConfigurationError("Broker configuration invalid") from None
