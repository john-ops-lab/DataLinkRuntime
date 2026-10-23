"""One versioned Kubernetes scope identity, including the effective CA trust set.

No token, credential reference, database state or network discovery participates.
The transport pins ``effective_ca_bytes`` after checking this same identity.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import ssl
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import certifi
import rfc8785

CA_BUNDLE_MAX_BYTES = 1024**2
SUPPORTED_RESOURCES = frozenset({("", "v1", "pods"), ("apps", "v1", "deployments")})


class KubernetesIdentityError(ValueError):
    """Only a fixed code crosses the configuration/transport boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def normalized_origin(server: object) -> str:
    try:
        if not isinstance(server, str) or any(char.isspace() for char in server):
            raise ValueError
        parsed = urlsplit(server)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "%" in parsed.hostname
        ):
            raise ValueError
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        if ":" in hostname:
            hostname = f"[{ipaddress.IPv6Address(hostname).compressed}]"
        port = parsed.port
        if port is not None and not 0 < port <= 65535:
            raise ValueError
        return "https://" + hostname + (f":{port}" if port not in {None, 443} else "")
    except (ValueError, UnicodeError) as error:
        raise KubernetesIdentityError("source_server_invalid") from error


def effective_ca_bytes(config: Mapping[str, object]) -> bytes:
    """Match Kubernetes 35's explicit CA / certifi fallback, with a finite read."""
    try:
        ca = config.get("ca_pem")
        if ca is None:
            with Path(certifi.where()).open("rb") as stream:
                result = stream.read(CA_BUNDLE_MAX_BYTES + 1)
        elif isinstance(ca, str) and len(ca) <= 65536:
            result = ca.encode("utf-8")
        else:
            raise ValueError
        if not result or len(result) > CA_BUNDLE_MAX_BYTES:
            raise ValueError
        return result
    except (OSError, ValueError, UnicodeError) as error:
        raise KubernetesIdentityError("source_trust_invalid") from error


def ca_trust_sha256(bundle: bytes) -> str:
    try:
        if not bundle or len(bundle) > CA_BUNDLE_MAX_BYTES:
            raise ValueError
        # Verify the exact bytes accepted by the TLS transport, then canonicalize
        # certificate DER sets so PEM wrapping/order/duplicates do not change trust.
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=bundle.decode("ascii"))
        # OpenSSL above validates the actual trust bytes. The stdlib PEM decoder
        # retains certificate DER exactly, including legacy roots accepted by TLS.
        certificates = re.findall(
            rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", bundle, re.DOTALL
        )
        if not certificates:
            raise ValueError
        hashes = sorted(
            {
                hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.decode("ascii"))).hexdigest()
                for cert in certificates
            }
        )
        return hashlib.sha256(rfc8785.dumps(hashes)).hexdigest()
    except (ValueError, UnicodeError, ssl.SSLError) as error:
        raise KubernetesIdentityError("source_trust_invalid") from error


def kubernetes_scope_identity(config: Mapping[str, object]) -> dict[str, object]:
    return identity_with_ca(config, effective_ca_bytes(config))


def identity_with_ca(config: Mapping[str, object], bundle: bytes) -> dict[str, object]:
    """Transport uses the same bytes for the check and its private CA file."""
    origin = normalized_origin(config.get("server"))
    trust = ca_trust_sha256(bundle)
    fields = ("cluster_id", "group", "version", "resource", "label_selector", "field_selector")
    values = {key: config.get(key, "") for key in fields}
    namespace = config.get("namespace")
    if (
        any(not isinstance(value, str) for value in values.values())
        or not values["cluster_id"]
        or (namespace is not None and not isinstance(namespace, str))
    ):
        raise KubernetesIdentityError("source_scope_invalid")
    scope = {
        "version": 1,
        "origin": origin,
        "trust_sha256": trust,
        "cluster_id": values["cluster_id"],
        "gvr": [values["group"], values["version"], values["resource"]],
        "namespace": namespace,
        "label_selector": values["label_selector"],
        "field_selector": values["field_selector"],
    }
    try:
        fingerprint = hashlib.sha256(rfc8785.dumps(cast(Any, scope))).hexdigest()
    except (ValueError, TypeError, UnicodeError) as error:
        raise KubernetesIdentityError("source_scope_invalid") from error
    return {"version": 1, "origin": origin, "trust_sha256": trust, "scope_fingerprint": fingerprint}


def source_identity(snapshot: Mapping[str, object]) -> str:
    return "kubernetes-scope-v1:" + str(snapshot["scope_fingerprint"])
