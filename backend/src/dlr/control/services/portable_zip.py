"""Bounded in-memory ZIP codec. Never extract paths or execute package content."""

import base64
import binascii
import io
import json
import stat
import struct
import time
import zipfile
from pathlib import PurePosixPath
from typing import Any

from pydantic import ValidationError

from dlr.control.schemas.portable import PortablePackage
from dlr.control.services.adapter import domain_error

MAX_ZIP_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 32
MAX_SECONDS = 5
CODE_NAMES = {"python": "python.py", "javascript": "javascript.mjs", "java": "java.java"}


def invalid() -> None:
    raise domain_error(422, "portable_package_invalid", "Invalid or unsupported DLR package")


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _safe_path(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not (
        path.is_absolute()
        or "\\" in name
        or ":" in name
        or "\x00" in name
        or any(part in {"", ".", ".."} for part in name.split("/"))
    )


def _check_directory(content: bytes) -> None:
    # Reject ZIP64, multi-disk archives and excessive directory records before
    # ZipFile allocates ZipInfo objects for every attacker-controlled entry.
    eocd = content.rfind(b"PK\x05\x06", max(0, len(content) - 65557))
    if eocd < 0 or len(content) - eocd < 22:
        invalid()
    _, disk, directory_disk, disk_count, count, size, offset, comment = struct.unpack_from(
        "<4s4H2LH", content, eocd
    )
    if (
        disk
        or directory_disk
        or disk_count != count
        or count > MAX_ENTRIES
        or eocd + 22 + comment != len(content)
    ):
        invalid()
    if offset + size != eocd:
        invalid()
    position = offset
    for _ in range(count):
        if position + 46 > eocd or content[position : position + 4] != b"PK\x01\x02":
            invalid()
        name_size, extra_size, comment_size = struct.unpack_from("<3H", content, position + 28)
        if name_size > 1024 or extra_size > 4096 or comment_size > 4096:
            invalid()
        position += 46 + name_size + extra_size + comment_size
    if position != eocd:
        invalid()


def encode_package(package: PortablePackage) -> bytes:
    data = package.model_dump(mode="json")
    entries: dict[str, bytes] = {}
    for variant in data["variants"]:
        path = "code/" + CODE_NAMES[variant["language"]]
        entries[path] = variant.pop("code").encode("utf-8")
        variant["code_file"] = path
    for index, file in enumerate(data["input"]["files"]):
        path = f"input/{index}/{file['filename']}"
        if not _safe_path(path) or PurePosixPath(file["filename"]).name != file["filename"]:
            invalid()
        try:
            entries[path] = base64.b64decode(file.pop("data_base64"), validate=True)
        except (ValueError, binascii.Error):
            invalid()
        file["content_file"] = path
    entries["manifest.json"] = json.dumps(
        data, ensure_ascii=False, indent=2, allow_nan=False
    ).encode()
    if sum(map(len, entries.values())) > MAX_EXPANDED_BYTES or len(entries) > MAX_ENTRIES:
        raise domain_error(413, "portable_package_too_large", "DLR package is too large")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    result = buffer.getvalue()
    if len(result) > MAX_ZIP_BYTES:
        raise domain_error(413, "portable_package_too_large", "DLR package is too large")
    return result


def decode_package(content: bytes) -> PortablePackage:
    if len(content) > MAX_ZIP_BYTES:
        raise domain_error(413, "portable_package_too_large", "DLR package is too large")
    deadline = time.monotonic() + MAX_SECONDS
    try:
        _check_directory(content)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_EXPANDED_BYTES:
                invalid()
            entries: dict[str, bytes] = {}
            total = 0
            for info in infos:
                mode = info.external_attr >> 16
                if (
                    not _safe_path(info.filename)
                    or info.filename in entries
                    or info.is_dir()
                    or info.flag_bits & 1
                    or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                    or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                ):
                    invalid()
                parts = []
                with archive.open(info) as handle:
                    while chunk := handle.read(65536):
                        total += len(chunk)
                        if total > MAX_EXPANDED_BYTES or time.monotonic() > deadline:
                            invalid()
                        parts.append(chunk)
                entries[info.filename] = b"".join(parts)
            manifest = json.loads(
                entries.pop("manifest.json"),
                object_pairs_hook=_json_object,
                parse_constant=lambda _: invalid(),
            )
            if not isinstance(manifest, dict) or not isinstance(manifest["variants"], list):
                invalid()
            for variant in manifest["variants"]:
                if not isinstance(variant, dict) or "code" in variant:
                    invalid()
                variant["code"] = entries.pop(variant.pop("code_file")).decode("utf-8")
            files = manifest["input"]["files"]
            if not isinstance(files, list):
                invalid()
            for file in files:
                if not isinstance(file, dict) or "data_base64" in file:
                    invalid()
                file["data_base64"] = base64.b64encode(
                    entries.pop(file.pop("content_file"))
                ).decode()
            if entries:
                invalid()
            if time.monotonic() > deadline:
                invalid()
            result = PortablePackage.model_validate(manifest)
            # Validate filenames and round-trip size constraints through the same encoder.
            encode_package(result)
            return result
    except (
        struct.error,
        zipfile.BadZipFile,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        RuntimeError,
        UnicodeError,
        ValidationError,
        RecursionError,
        NotImplementedError,
    ):
        invalid()
    raise AssertionError("unreachable")
