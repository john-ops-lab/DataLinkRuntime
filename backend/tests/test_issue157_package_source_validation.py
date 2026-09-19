"""Issue #157 package-source syntax, atomicity, and safe-error regressions."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import PackageSource
from dlr.control.services import package_source as package_source_service


def _create(
    client: TestClient,
    *,
    name: str,
    kind: str = "pypi",
    index_url: object = "https://packages.example.invalid/simple/",
    is_default: bool = False,
) -> Any:
    return client.post(
        "/api/package-sources",
        json={
            "name": name,
            "kind": kind,
            "index_url": index_url,
            "is_default": is_default,
        },
    )


@pytest.mark.parametrize(
    ("kind", "index_url"),
    [
        ("pypi", "https://user:secret@packages.example.invalid:8443/simple/?channel=stable#top"),
        ("npm", "http://localhost:4873/npm/"),
        ("maven", "https://[2001:db8::1]:8443/repository/public?mirror=primary#section"),
        ("goproxy", "http://127.0.0.1:8080/go/"),
    ],
)
def test_four_source_kinds_accept_supported_single_http_urls(
    api_client: TestClient,
    kind: str,
    index_url: str,
) -> None:
    response = _create(api_client, name=f"valid-{kind}", kind=kind, index_url=index_url)

    assert response.status_code == 201, response.text
    assert response.json()["index_url"] == index_url


def test_http_url_with_empty_optional_port_keeps_existing_parser_semantics(
    api_client: TestClient,
) -> None:
    index_url = "https://packages.example.invalid:/simple/"

    response = _create(api_client, name="empty-port", index_url=index_url)

    assert response.status_code == 201, response.text
    assert response.json()["index_url"] == index_url


@pytest.mark.parametrize(
    ("name", "index_url"),
    [
        ("unicode-host", "https://用户.example.invalid/simple/"),
        ("underscore-host", "https://package_cache.example.invalid/simple/"),
        ("percent-host", "https://packages%2Eexample.invalid/simple/"),
        (
            "encoded-userinfo-backslash",
            "https://user:pass%5Cword@packages.example.invalid/simple/",
        ),
        ("empty-port-host", "https://localhost:/simple/?channel=stable#top"),
    ],
)
def test_host_validation_preserves_existing_compatible_forms(
    api_client: TestClient,
    name: str,
    index_url: str,
) -> None:
    response = _create(api_client, name=name, index_url=index_url)

    assert response.status_code == 201, response.text
    assert response.json()["index_url"] == index_url


@pytest.mark.parametrize("kind", ["pypi", "npm", "maven", "goproxy"])
def test_four_source_kinds_accept_only_their_exact_builtin_url(
    api_client: TestClient,
    kind: str,
) -> None:
    accepted = _create(
        api_client,
        name=f"builtin-{kind}",
        kind=kind,
        index_url=f"dlr-builtin://{kind}",
    )
    rejected = _create(
        api_client,
        name=f"mismatch-{kind}",
        kind=kind,
        index_url="dlr-builtin://npm" if kind != "npm" else "dlr-builtin://pypi",
    )

    assert accepted.status_code == 201, accepted.text
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "builtin_source_invalid"


@pytest.mark.parametrize("kind", ["pypi", "npm", "maven", "goproxy"])
@pytest.mark.parametrize(
    ("index_url", "reason"),
    [
        ("not-a-url", "scheme"),
        ("https:///repository", "host"),
        ("file://packages.example.invalid/repository", "scheme"),
        ("https://packages.example.invalid:bad/repository", "invalid_authority"),
        ("https://packages.example.invalid:70000/repository", "invalid_authority"),
        (r"http://host\evil.invalid/repository", "host_syntax"),
        (r"https://user:pass\@packages.example.invalid/repository", "host_syntax"),
        ("https://host^evil.invalid/repository", "host_syntax"),
        ("https://host%zz.invalid/repository", "host_syntax"),
        (" https://packages.example.invalid/repository", "whitespace_or_control"),
        ("https://packages.example.invalid/repo\tsecret", "whitespace_or_control"),
        ("\x00https://packages.example.invalid/repository", "whitespace_or_control"),
    ],
)
def test_four_source_kinds_reject_invalid_urls_with_stable_field_error(
    api_client: TestClient,
    kind: str,
    index_url: str,
    reason: str,
) -> None:
    response = _create(api_client, name=f"invalid-{kind}", kind=kind, index_url=index_url)

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "package_source_url_invalid",
            "message": "Package source index URL is invalid",
            "params": {"field": "index_url", "reason": reason},
        }
    }
    assert index_url not in response.text


@pytest.mark.parametrize("kind", ["pypi", "npm", "maven", "goproxy"])
def test_patch_rejects_malformed_host_without_mutating_source(
    api_client: TestClient,
    kind: str,
) -> None:
    source = _create(
        api_client,
        name=f"patch-host-{kind}",
        kind=kind,
        index_url="https://before.example.invalid/repository",
    ).json()

    response = api_client.patch(
        f"/api/package-sources/{source['id']}",
        json={
            "name": "must-not-stick",
            "index_url": r"https://user:pass\@packages.example.invalid/repository",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["params"]["reason"] == "host_syntax"
    unchanged = api_client.get("/api/package-sources").json()
    persisted = next(item for item in unchanged if item["id"] == source["id"])
    assert persisted["name"] == source["name"]
    assert persisted["index_url"] == source["index_url"]


@pytest.mark.parametrize(
    "index_url",
    [
        "direct",
        "off",
        "https://one.example.invalid,https://two.example.invalid",
        "https://one.example.invalid|https://two.example.invalid",
    ],
)
def test_goproxy_keeps_single_url_product_contract(
    api_client: TestClient,
    index_url: str,
) -> None:
    response = _create(api_client, name="invalid-goproxy", kind="goproxy", index_url=index_url)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "package_source_url_invalid"


def test_create_and_patch_do_not_probe_the_network(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_probe(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("saving a package source must not probe the network")

    monkeypatch.setattr(package_source_service, "probe_index_url", unexpected_probe)
    created = _create(
        api_client,
        name="offline-save",
        index_url="https://offline.example.invalid/simple/",
    )
    assert created.status_code == 201, created.text

    updated = api_client.patch(
        f"/api/package-sources/{created.json()['id']}",
        json={"index_url": "http://offline.example.invalid:8080/simple/?v=2#index"},
    )
    assert updated.status_code == 200, updated.text


def test_patch_validates_complete_configuration_before_any_mutation(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    source = _create(
        api_client,
        name="atomic-source",
        kind="pypi",
        index_url="https://packages.example.invalid/simple,legacy-compatible",
        is_default=True,
    ).json()
    other = _create(
        api_client,
        name="other-go-source",
        kind="goproxy",
        index_url="https://go.example.invalid/",
        is_default=True,
    ).json()

    invalid_address = api_client.patch(
        f"/api/package-sources/{source['id']}",
        json={"index_url": "not-a-url"},
    )
    assert invalid_address.status_code == 422

    rejected = api_client.patch(
        f"/api/package-sources/{source['id']}",
        json={
            "name": "must-not-stick",
            "kind": "goproxy",
            "is_default": True,
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["params"]["reason"] == "multiple_sources"

    with session_factory() as session:
        unchanged = session.get(PackageSource, source["id"])
        other_default = session.get(PackageSource, other["id"])
        assert unchanged is not None
        assert unchanged.name == "atomic-source"
        assert unchanged.kind == "pypi"
        assert unchanged.index_url == "https://packages.example.invalid/simple,legacy-compatible"
        assert unchanged.is_default is True
        assert other_default is not None and other_default.is_default is True

    repaired = api_client.patch(
        f"/api/package-sources/{source['id']}",
        json={
            "name": "repaired-source",
            "kind": "goproxy",
            "index_url": "https://go.example.invalid/proxy/",
            "is_default": False,
        },
    )
    assert repaired.status_code == 200, repaired.text
    assert repaired.json()["kind"] == "goproxy"
    assert repaired.json()["index_url"] == "https://go.example.invalid/proxy/"


def test_patch_credential_failure_cannot_leave_name_url_or_default_changes(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    token = api_client.post(
        "/api/credentials",
        json={
            "name": "npm-only-token",
            "type": "token",
            "fields": {"token": "SYNTHETIC_TOKEN_VALUE"},
        },
    ).json()
    source = _create(
        api_client,
        name="credential-atomic-source",
        kind="pypi",
        index_url="https://before.example.invalid/simple/",
        is_default=False,
    ).json()

    rejected = api_client.patch(
        f"/api/package-sources/{source['id']}",
        json={
            "name": "must-not-stick",
            "index_url": "https://after.example.invalid/simple/",
            "credential_id": token["id"],
            "is_default": True,
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "package_source_credential_incompatible"

    with session_factory() as session:
        unchanged = session.get(PackageSource, source["id"])
        assert unchanged is not None
        assert unchanged.name == "credential-atomic-source"
        assert unchanged.index_url == "https://before.example.invalid/simple/"
        assert unchanged.credential_id is None
        assert unchanged.is_default is False


def test_patch_preserves_omitted_and_explicit_null_semantics(api_client: TestClient) -> None:
    source = _create(
        api_client,
        name="null-contract",
        kind="npm",
        index_url="https://registry.example.invalid/",
        is_default=True,
    ).json()

    response = api_client.patch(
        f"/api/package-sources/{source['id']}",
        json={"name": None, "kind": None, "index_url": None, "is_default": None},
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == source["name"]
    assert response.json()["kind"] == source["kind"]
    assert response.json()["index_url"] == source["index_url"]
    assert response.json()["is_default"] is True


def test_legacy_invalid_source_can_be_listed_repaired_or_deleted(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        repair = PackageSource(
            name="legacy-repair",
            kind="pypi",
            index_url="not-a-url",
            is_default=False,
        )
        remove = PackageSource(
            name="legacy-delete",
            kind="npm",
            index_url="also-not-a-url",
            is_default=False,
        )
        session.add_all([repair, remove])
        session.commit()
        repair_id = repair.id
        remove_id = remove.id

    listed = api_client.get("/api/package-sources")
    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()} >= {repair_id, remove_id}

    still_invalid = api_client.patch(
        f"/api/package-sources/{repair_id}", json={"name": "legacy-still-invalid"}
    )
    assert still_invalid.status_code == 422
    repaired = api_client.patch(
        f"/api/package-sources/{repair_id}",
        json={"index_url": "https://repaired.example.invalid/simple/"},
    )
    assert repaired.status_code == 200, repaired.text
    assert api_client.delete(f"/api/package-sources/{remove_id}").status_code == 204


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "wrong-type", "kind": "pypi", "index_url": {"secret": "NO_ECHO_OBJECT"}},
        {"name": "wrong-kind", "kind": "NO_ECHO_KIND", "index_url": "https://valid.invalid/"},
    ],
)
def test_package_source_request_validation_does_not_echo_input(
    api_client: TestClient,
    caplog: pytest.LogCaptureFixture,
    payload: dict[str, object],
) -> None:
    caplog.set_level(logging.INFO)
    response = _create(
        api_client,
        name=str(payload["name"]),
        kind=str(payload["kind"]),
        index_url=payload["index_url"],
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "package_source_request_invalid"
    for secret in ("NO_ECHO_OBJECT", "NO_ECHO_KIND"):
        assert secret not in response.text
        assert secret not in caplog.text


def test_malformed_package_source_body_has_value_free_error(api_client: TestClient) -> None:
    secret = "NO_ECHO_MALFORMED"
    response = api_client.post(
        "/api/package-sources",
        content=f'{{"name":"broken","index_url":"{secret}"',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "package_source_request_invalid",
        "message": "Package source request is invalid",
        "params": {"field": "request", "reason": "request_validation"},
    }
    assert secret not in response.text


def test_semantic_url_error_does_not_echo_userinfo_or_query_secrets(
    api_client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    userinfo_secret = "NO_ECHO_USERINFO"
    query_secret = "NO_ECHO_QUERY"
    invalid_url = (
        f"https://user:{userinfo_secret}@packages.example.invalid:bad/repository"
        f"?token={query_secret}#section"
    )
    caplog.set_level(logging.INFO)

    response = _create(api_client, name="safe-semantic-error", index_url=invalid_url)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "package_source_url_invalid"
    for secret in (userinfo_secret, query_secret, invalid_url):
        assert secret not in response.text
        assert secret not in caplog.text


def test_invalid_patch_path_and_body_share_value_free_route_handler(api_client: TestClient) -> None:
    secret = "NO_ECHO_PATCH_BODY"
    response = api_client.patch(
        "/api/package-sources/not-an-id",
        json={"index_url": {"secret": secret}},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "package_source_request_invalid"
    assert secret not in response.text
