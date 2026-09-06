"""Bounded, non-executing inspection of offline installation materials."""

import email.parser
import gzip
import io
import json
import re
import struct
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

KINDS = {"python": "pypi", "javascript": "npm", "java": "maven"}
MAX_METADATA = 1024 * 1024
MAX_MEMBERS = 20000
MAX_EXPANDED = 1024 * 1024 * 1024


class PackageValidationError(ValueError):
    pass


class _BoundedTarReader(io.BufferedIOBase):
    """Bound PAX header allocations before tarfile parses member metadata."""

    def __init__(self, stream: gzip.GzipFile) -> None:
        self.stream = stream

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0 or size > MAX_METADATA:
            raise PackageValidationError("tar metadata exceeds allocation limit")
        return self.stream.read(size)

    def tell(self) -> int:
        return self.stream.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        target = offset if whence == 0 else self.tell() + offset
        if whence not in (0, 1) or target > MAX_EXPANDED + MAX_MEMBERS * 1024:
            raise PackageValidationError("tar exceeds expansion limit")
        return self.stream.seek(offset, whence)


def safe_path(value: str, *, archive_member: bool = False) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 512
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
        or "\\" in value
        or any(ord(char) < 32 for char in value)
        or (not archive_member and not re.fullmatch(r"[A-Za-z0-9_@.+/=-]+", value))
    ):
        raise PackageValidationError("invalid repository path")
    return value


def _xml(data: bytes) -> ElementTree.Element:
    if (
        len(data) > MAX_METADATA
        or b"\x00" in data
        or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", data, re.IGNORECASE)
    ):
        raise PackageValidationError("XML metadata exceeds limits or contains a declaration")
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise PackageValidationError("invalid XML metadata") from error


def validate_npm_dependencies(package: dict[str, Any]) -> None:
    """Only registry version/range specifications; npm remains the resolver."""
    for field in ("dependencies", "optionalDependencies", "peerDependencies"):
        dependencies = package.get(field, {})
        if not isinstance(dependencies, dict):
            raise PackageValidationError("invalid npm dependencies")
        for name, spec in dependencies.items():
            if not re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+", name):
                raise PackageValidationError("invalid npm package name")
            if not isinstance(spec, str) or not re.fullmatch(r"[0-9xXvV*~^<>=| .+-]+", spec):
                raise PackageValidationError("builtin source requires npm registry version ranges")


def inspect_package(
    path: Path, kind: str, filename: str, repository_path: str = ""
) -> dict[str, Any]:
    safe_path(filename)
    if "/" in filename:
        raise PackageValidationError("filename must not contain directories")
    metadata: dict[str, Any] = {}
    if kind == "pypi" and filename.endswith(".whl"):
        with _bounded_zip(path) as archive:
            members = archive.infolist()
            _validate_zip(members)
            names = [m for m in members if m.filename.endswith(".dist-info/METADATA")]
            if len(names) != 1 or names[0].file_size > MAX_METADATA:
                raise PackageValidationError("wheel requires one bounded METADATA")
            parsed = email.parser.BytesParser().parsebytes(archive.read(names[0]))
            name, version = parsed.get("Name"), parsed.get("Version")
            parts = filename[:-4].split("-")
            if len(parts) not in (5, 6) or not name or not version:
                raise PackageValidationError("invalid wheel metadata")

            def normalize(value: str) -> str:
                return re.sub(r"[-_.]+", "-", value).lower()

            if normalize(parts[0]) != normalize(name) or normalize(parts[1]) != normalize(version):
                raise PackageValidationError("wheel filename and metadata disagree")
            # URL requirements are forbidden even when hidden in a wheel's metadata.
            for requirement in parsed.get_all("Requires-Dist", []):
                if "@" in requirement or "://" in requirement:
                    raise PackageValidationError("wheel contains an external dependency URL")
            name = normalize(name)
            environment = "-".join(parts[-3:])
            metadata = {
                "requires_python": parsed.get("Requires-Python"),
                "dependencies": parsed.get_all("Requires-Dist", []),
            }
        repository_path = filename
    elif kind == "npm" and filename.endswith((".tgz", ".tar.gz")):
        with (
            gzip.open(path, "rb") as compressed,
            tarfile.open(fileobj=_BoundedTarReader(compressed), mode="r:") as tar_archive,
        ):
            expanded = 0
            package_data = None
            for count, member in enumerate(tar_archive, start=1):
                expanded += member.size
                if count > MAX_MEMBERS or expanded > MAX_EXPANDED:
                    raise PackageValidationError("archive exceeds expansion limit")
                safe_path(member.name.rstrip("/"), archive_member=True)
                if member.name == "package/npm-shrinkwrap.json" or "/node_modules/" in member.name:
                    raise PackageValidationError(
                        "upload unbundled npm materials without embedded shrinkwrap URLs"
                    )
                if not member.name.startswith("package/") or not (
                    member.isfile() or member.isdir()
                ):
                    raise PackageValidationError("npm archive has an unsafe member")
                if member.name == "package/package.json":
                    if package_data is not None or member.size > MAX_METADATA:
                        raise PackageValidationError("npm requires one bounded package.json")
                    stream = tar_archive.extractfile(member)
                    if stream is None:
                        raise PackageValidationError("missing package.json")
                    package_data = json.loads(stream.read(MAX_METADATA + 1))
            if not isinstance(package_data, dict):
                raise PackageValidationError("npm archive is missing package.json")
            name, version = package_data.get("name"), package_data.get("version")
            if not isinstance(name, str) or not re.fullmatch(
                r"(?:@[a-z0-9._-]+/)?[a-z0-9._-]+", name
            ):
                raise PackageValidationError("invalid npm name")
            if not isinstance(version, str) or not re.fullmatch(
                r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version
            ):
                raise PackageValidationError("invalid npm version")
            validate_npm_dependencies(package_data)
            # Bundled packages can contain their own external references.
            if any(package_data.get(key) for key in ("bundleDependencies", "bundledDependencies")):
                raise PackageValidationError("upload unbundled npm packages and their dependencies")
            metadata = {
                key: package_data[key]
                for key in (
                    "name",
                    "version",
                    "dependencies",
                    "optionalDependencies",
                    "peerDependencies",
                    "peerDependenciesMeta",
                    "engines",
                    "os",
                    "cpu",
                    "libc",
                )
                if key in package_data
            }
            environment = json.dumps(
                {key: metadata[key] for key in ("engines", "os", "cpu", "libc") if key in metadata},
                sort_keys=True,
            )
            repository_path = f"{name}/{version}/{filename}"
    elif kind == "maven":
        safe_path(repository_path)
        parts = repository_path.split("/")
        if parts[-1] != filename or len(parts) < 4:
            raise PackageValidationError(
                "Maven requires group/artifact/version/filename repository path"
            )
        group, artifact, version = ".".join(parts[:-3]), parts[-3], parts[-2]
        name = f"{group}:{artifact}"
        environment = filename
        if filename.endswith(".jar"):
            if not filename.startswith(f"{artifact}-{version}"):
                raise PackageValidationError("JAR path and filename disagree")
            with _bounded_zip(path) as archive:
                _validate_zip(archive.infolist())
        elif filename.endswith(".pom"):
            if filename != f"{artifact}-{version}.pom":
                raise PackageValidationError("POM path and filename disagree")
            root = _xml(path.read_bytes() if path.stat().st_size <= MAX_METADATA else b"<!")

            def value(tag: str) -> str | None:
                return root.findtext(f"{{*}}{tag}") or root.findtext(f"{{*}}parent/{{*}}{tag}")

            if (
                value("groupId") != group
                or value("artifactId") != artifact
                or value("version") != version
            ):
                raise PackageValidationError("POM coordinates and repository path disagree")
        elif filename.startswith("maven-metadata") and filename.endswith(".xml"):
            _xml(path.read_bytes() if path.stat().st_size <= MAX_METADATA else b"<!")
        else:
            raise PackageValidationError("unsupported Maven material")
    else:
        raise PackageValidationError("expected wheel, npm tgz, or Maven JAR/POM/metadata XML")
    return {
        "name": name,
        "version": version,
        "environment": environment,
        "repository_path": safe_path(repository_path),
        "metadata": metadata,
    }


def _bounded_zip(path: Path) -> zipfile.ZipFile:
    # Bound the central directory before ZipFile allocates one ZipInfo per entry.
    with path.open("rb") as stream:
        size = path.stat().st_size
        stream.seek(max(0, size - 65557))
        tail = stream.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise PackageValidationError("invalid ZIP directory")
    fields = struct.unpack_from("<4s4H2LH", tail, offset)
    if (
        fields[1]
        or fields[2]
        or fields[3] != fields[4]
        or fields[4] > MAX_MEMBERS
        or fields[5] > 8 * MAX_METADATA
    ):
        raise PackageValidationError("ZIP directory exceeds limits")
    return zipfile.ZipFile(path)


def _validate_zip(members: list[zipfile.ZipInfo]) -> None:
    if len(members) > MAX_MEMBERS or sum(member.file_size for member in members) > MAX_EXPANDED:
        raise PackageValidationError("archive exceeds expansion limit")
    seen = set()
    for member in members:
        safe_path(member.orig_filename.rstrip("/"), archive_member=True)
        if member.filename in seen or (member.external_attr >> 16) & 0o170000 == 0o120000:
            raise PackageValidationError("archive contains duplicate paths or symbolic links")
        seen.add(member.filename)
