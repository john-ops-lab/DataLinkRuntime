"""Host-key-verified, root-confined SFTP list and bounded read."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import posixpath
import socket
import stat
from contextlib import suppress

_STABLE_ERRORS = frozenset(
    {
        "input_must_be_object",
        "host_username_fingerprint_and_base_required",
        "invalid_read_paths",
        "host_key_mismatch",
        "invalid_private_key",
        "missing_credential",
        "invalid_path",
        "path_escape",
        "invalid_suffix",
        "invalid_start_at",
        "invalid_checkpoint",
        "max_total_bytes_too_small",
    }
)


def _positive(value: object, default: int, maximum: int) -> int:
    return (
        min(value, maximum)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else default
    )


def _inside(base: str, candidate: str) -> bool:
    return candidate == base or candidate.startswith(base.rstrip("/") + "/")


def _encoded_size(value: object) -> int:
    import json

    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _array_size(count: int, item_bytes: int) -> int:
    return 2 if count == 0 else item_bytes + count + 1


def _checkpoint(start_at: str | None, reason: str | None = None):
    value = {"start_at": start_at}
    if reason is not None:
        value["reason"] = reason
    return value


def _result(
    files,
    contents,
    total_bytes: int,
    partial: bool,
    checkpoint,
    *,
    file_count: int | None = None,
):
    return {
        "files": files,
        "contents": contents,
        "summary": {
            "files": len(files) if file_count is None else file_count,
            "bytes_read": total_bytes,
        },
        "partial": partial,
        "checkpoint": checkpoint,
    }


def _result_size(
    file_count: int,
    file_item_bytes: int,
    content_count: int,
    content_item_bytes: int,
    total_bytes: int,
    partial: bool,
    checkpoint,
) -> int:
    shell = _result(
        [],
        [],
        total_bytes,
        partial,
        checkpoint,
        file_count=file_count,
    )
    return (
        _encoded_size(shell)
        - 4
        + _array_size(file_count, file_item_bytes)
        + _array_size(content_count, content_item_bytes)
    )


def _read_content_size(path: str, raw_bytes: int) -> int:
    shell = {
        "path": path,
        "status": "read",
        "bytes": raw_bytes,
        "content_base64": "",
    }
    return _encoded_size(shell) + 4 * ((raw_bytes + 2) // 3)


def _raw_capacity(
    path: str,
    maximum: int,
    file_count: int,
    file_item_bytes: int,
    content_count: int,
    content_item_bytes: int,
    total_bytes: int,
    checkpoint,
    max_total_bytes: int,
) -> int:
    low, high = 0, maximum
    while low < high:
        candidate = (low + high + 1) // 2
        candidate_content_bytes = content_item_bytes + _read_content_size(path, candidate)
        if (
            _result_size(
                file_count,
                file_item_bytes,
                content_count + 1,
                candidate_content_bytes,
                total_bytes + candidate,
                True,
                checkpoint,
            )
            <= max_total_bytes
        ):
            low = candidate
        else:
            high = candidate - 1
    return low


def _handle(context, input):
    if not isinstance(input, dict):
        raise ValueError("input_must_be_object")
    import paramiko

    host = input.get("host")
    username = context.secrets.get("SFTP_USERNAME") or input.get("username")
    fingerprint = input.get("host_fingerprint_sha256")
    base_directory = input.get("base_directory", "/")
    if not all(
        isinstance(value, str) and value for value in (host, username, fingerprint, base_directory)
    ):
        raise ValueError("host_username_fingerprint_and_base_required")
    port = _positive(input.get("port"), 22, 65_535)
    max_files = _positive(input.get("max_files"), 500, 5_000)
    max_file_bytes = _positive(input.get("max_file_bytes"), 1_048_576, 8_388_608)
    max_total_bytes = _positive(input.get("max_total_bytes"), 4_194_304, 16_777_216)
    start_at = input.get("start_at")
    if start_at is not None and (not isinstance(start_at, str) or not start_at):
        raise ValueError("invalid_start_at")
    if (
        max_total_bytes < 256
        or _result_size(
            0,
            0,
            0,
            0,
            0,
            True,
            _checkpoint(start_at, "checkpoint_limit"),
        )
        > max_total_bytes
    ):
        raise ValueError("max_total_bytes_too_small")
    raw_read_paths = input.get("read_paths", [])
    if (
        not isinstance(raw_read_paths, list)
        or len(raw_read_paths) > 5_000
        or not all(isinstance(path, str) for path in raw_read_paths)
    ):
        raise ValueError("invalid_read_paths")
    read_paths = set(raw_read_paths)
    sock = None
    transport = None
    sftp = None
    try:
        sock = socket.create_connection((host, port), timeout=20)
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=20)
        server_key = transport.get_remote_server_key()
        actual = "SHA256:" + base64.b64encode(
            hashlib.sha256(server_key.asbytes()).digest()
        ).decode().rstrip("=")
        if not hmac.compare_digest(actual, fingerprint):
            raise ValueError("host_key_mismatch")
        password = context.secrets.get("SFTP_PASSWORD")
        private_key = context.secrets.get("SFTP_PRIVATE_KEY")
        if private_key:
            key = None
            passphrase = context.secrets.get("SFTP_PRIVATE_KEY_PASSPHRASE")
            for key_type in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
                try:
                    key = key_type.from_private_key(io.StringIO(private_key), password=passphrase)
                    break
                except (paramiko.SSHException, ValueError):
                    continue
            if key is None:
                raise ValueError("invalid_private_key")
            transport.auth_publickey(username, key)
        elif password:
            transport.auth_password(username, password)
        else:
            raise ValueError("missing_credential")
        sftp = paramiko.SFTPClient.from_transport(transport)
        base = posixpath.normpath(sftp.normalize(base_directory))
        requested = input.get("path", ".")
        if not isinstance(requested, str):
            raise ValueError("invalid_path")
        directory = posixpath.normpath(sftp.normalize(posixpath.join(base, requested)))
        if not _inside(base, directory):
            raise ValueError("path_escape")
        glob_suffix = input.get("suffix")
        if glob_suffix is not None and not isinstance(glob_suffix, str):
            raise ValueError("invalid_suffix")
        files: list[dict[str, object]] = []
        contents: list[dict[str, object]] = []
        file_item_bytes = 0
        content_item_bytes = 0
        total_bytes = 0
        partial = False
        entries = []
        start_found = start_at is None
        for entry in sftp.listdir_iter(directory, read_aheads=1):
            candidate = posixpath.join(directory, entry.filename)
            resolved = posixpath.normpath(sftp.normalize(candidate))
            if not _inside(base, resolved):
                continue
            if glob_suffix and not entry.filename.endswith(glob_suffix):
                continue
            if not stat.S_ISREG(entry.st_mode):
                continue
            relative = posixpath.relpath(resolved, base)
            if not start_found:
                if relative != start_at:
                    continue
                start_found = True
            entries.append((entry, resolved, relative))
            if len(entries) > max_files:
                partial = True
                break
        if not start_found:
            raise ValueError("invalid_checkpoint")
        output_checkpoint = None
        stopped = False
        for item_at, (entry, resolved, relative) in enumerate(entries[:max_files]):
            current_checkpoint = _checkpoint(relative, "output_limit")
            fallback_checkpoint = _checkpoint(start_at, "output_limit")
            if (
                _result_size(
                    len(files),
                    file_item_bytes,
                    len(contents),
                    content_item_bytes,
                    total_bytes,
                    True,
                    current_checkpoint,
                )
                > max_total_bytes
            ):
                current_checkpoint = fallback_checkpoint
            next_path = entries[item_at + 1][2] if item_at + 1 < len(entries) else None
            after_checkpoint = (
                _checkpoint(next_path, "checkpoint_limit") if next_path is not None else None
            )
            metadata = {
                "path": relative,
                "size": entry.st_size,
                "mtime": entry.st_mtime,
            }
            candidate_file_bytes = file_item_bytes + _encoded_size(metadata)
            candidate_content = None
            if relative not in read_paths:
                if (
                    _result_size(
                        len(files) + 1,
                        candidate_file_bytes,
                        len(contents),
                        content_item_bytes,
                        total_bytes,
                        next_path is not None or partial,
                        after_checkpoint,
                    )
                    > max_total_bytes
                ):
                    partial = True
                    output_checkpoint = current_checkpoint
                    stopped = True
                    break
                files.append(metadata)
                file_item_bytes = candidate_file_bytes
                continue
            if entry.st_size > max_file_bytes or total_bytes + entry.st_size > max_total_bytes:
                candidate_content = {
                    "path": relative,
                    "status": "limit_exceeded",
                    "size": entry.st_size,
                }
            if candidate_content is not None:
                candidate_content_bytes = content_item_bytes + _encoded_size(candidate_content)
                if (
                    _result_size(
                        len(files) + 1,
                        candidate_file_bytes,
                        len(contents) + 1,
                        candidate_content_bytes,
                        total_bytes,
                        True,
                        after_checkpoint,
                    )
                    > max_total_bytes
                ):
                    partial = True
                    output_checkpoint = current_checkpoint
                    stopped = True
                    break
                files.append(metadata)
                contents.append(candidate_content)
                file_item_bytes = candidate_file_bytes
                content_item_bytes = candidate_content_bytes
                partial = True
                continue
            read_limit = _raw_capacity(
                relative,
                max_file_bytes,
                len(files) + 1,
                candidate_file_bytes,
                len(contents),
                content_item_bytes,
                total_bytes,
                after_checkpoint,
                max_total_bytes,
            )
            if entry.st_size > read_limit:
                partial = True
                output_checkpoint = current_checkpoint
                stopped = True
                break
            with sftp.open(resolved, "rb") as handle:
                value = handle.read(read_limit + 1)
            if len(value) > read_limit:
                partial = True
                output_checkpoint = current_checkpoint
                stopped = True
                break
            candidate_content = {
                "path": relative,
                "status": "read",
                "bytes": len(value),
                "content_base64": base64.b64encode(value).decode(),
            }
            candidate_content_bytes = content_item_bytes + _encoded_size(candidate_content)
            if (
                _result_size(
                    len(files) + 1,
                    candidate_file_bytes,
                    len(contents) + 1,
                    candidate_content_bytes,
                    total_bytes + len(value),
                    next_path is not None or partial,
                    after_checkpoint,
                )
                > max_total_bytes
            ):
                partial = True
                output_checkpoint = current_checkpoint
                stopped = True
                break
            files.append(metadata)
            contents.append(candidate_content)
            file_item_bytes = candidate_file_bytes
            content_item_bytes = candidate_content_bytes
            total_bytes += len(value)
        if not stopped and len(entries) > max_files:
            partial = True
            output_checkpoint = _checkpoint(entries[max_files][2], "max_files")
        result = _result(files, contents, total_bytes, partial, output_checkpoint)
        if _encoded_size(result) > max_total_bytes:
            raise ValueError("max_total_bytes_too_small")
        return result
    except ValueError:
        raise
    except Exception:
        raise ValueError("sftp_operation_failed") from None
    finally:
        if sftp is not None:
            with suppress(Exception):
                sftp.close()
        if transport is not None:
            with suppress(Exception):
                transport.close()
        elif sock is not None:
            with suppress(Exception):
                sock.close()


def handle(context, input):
    try:
        return _handle(context, input)
    except ValueError as error:
        code = str(error)
        if code in _STABLE_ERRORS:
            raise ValueError(code) from None
        raise ValueError("sftp_operation_failed") from None
    except Exception:
        raise ValueError("sftp_operation_failed") from None
