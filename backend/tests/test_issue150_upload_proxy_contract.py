"""Deterministic proxy contract for policy-bounded Managed Input uploads."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from dlr.control.schemas.managed_input import MAX_FILE_BYTES
from dlr.control.services.multipart import MultipartReader

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
UPLOAD_LOCATION = r"^/api/adapters/\d+/input-artifacts$"
TOTAL_REQUEST_BYTES = 2_147_745_792
EXPECTED_BODY_LIMITS = Counter({"72m": 1, "48m": 1, "16g": 1, "2147745792": 1})
PROXY_HEADERS = (
    "proxy_set_header Host $host;",
    "proxy_set_header X-Real-IP $remote_addr;",
    "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
    "proxy_set_header X-Forwarded-Proto $scheme;",
)


def _config(name: str) -> str:
    return (REPOSITORY_ROOT / "docker" / name).read_text(encoding="utf-8")


def _location_body(config: str) -> str:
    marker = f"location ~ {UPLOAD_LOCATION} {{"
    assert config.count(marker) == 1
    body = config.split(marker, 1)[1].split("}", 1)[0]
    assert "location " not in body
    return body


def _body_limits(config: str) -> Counter[str]:
    return Counter(re.findall(r"client_max_body_size\s+([^;\s]+)\s*;", config))


def test_proxy_total_request_limit_tracks_application_contract() -> None:
    assert MAX_FILE_BYTES == 2 * 1024 * 1024 * 1024
    assert MultipartReader.REQUEST_OVERHEAD_LIMIT == 256 * 1024
    assert MAX_FILE_BYTES + MultipartReader.REQUEST_OVERHEAD_LIMIT == TOTAL_REQUEST_BYTES


@pytest.mark.parametrize(
    ("name", "account_entry"),
    [("nginx.conf", False), ("nginx-account.conf", True)],
)
def test_managed_input_upload_has_exact_streaming_proxy_boundary(
    name: str,
    account_entry: bool,
) -> None:
    config = _config(name)
    body = _location_body(config)

    assert "client_max_body_size 2147745792;" in body
    assert "client_body_timeout 60s;" in body
    assert "proxy_request_buffering off;" in body
    assert "proxy_http_version 1.1;" in body
    assert "proxy_read_timeout 300s;" in body
    assert "proxy_pass http://control:8000;" in body
    assert all(header in body for header in PROXY_HEADERS)
    assert _body_limits(config) == EXPECTED_BODY_LIMITS

    account_rewrite = "rewrite ^/api/(.*)$ /__dlr_account/api/$1 break;"
    assert (account_rewrite in body) is account_entry
    assert ("http {" in config) is account_entry


def test_upload_location_does_not_match_other_api_routes() -> None:
    route = re.compile(UPLOAD_LOCATION)

    assert route.fullmatch("/api/adapters/1/input-artifacts")
    assert route.fullmatch("/api/adapters/987654/input-artifacts")
    assert not route.fullmatch("/api/adapters/1/input-artifacts/")
    assert not route.fullmatch("/api/adapters/1/input-artifacts/2")
    assert not route.fullmatch("/api/adapters/1/input-config")
    assert not route.fullmatch("/api/adapters/x/input-artifacts")
