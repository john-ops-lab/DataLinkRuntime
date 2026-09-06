"""Issue #132 validated catalog, lazy loading and read-only API contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dlr.common.config import settings
from dlr.control.services import template as template_service
from dlr.control.template_catalog import CatalogValidationError, TemplateCatalog


def _copy_catalog(tmp_path: Path) -> Path:
    source = Path(str(resources.files("dlr.control.template_catalog")))
    target = tmp_path / "template_catalog"
    shutil.copytree(source, target)
    return target


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _first_metadata(root: Path) -> tuple[Path, dict[str, Any]]:
    catalog = _json(root / "catalog.json")
    path = root / catalog["scenarios"][0]["metadata_resource"]
    return path, _json(path)


def test_catalog_inventory_is_exact_and_all_sources_pass_hash_validation() -> None:
    catalog = TemplateCatalog(resources.files("dlr.control.template_catalog"))

    assert len(catalog.themes) == 5
    assert len(catalog.scenarios) == 17
    assert len({scenario.slug for scenario in catalog.scenarios}) == 17
    assert all(
        set(variant.language for variant in scenario.variants) <= {"python", "javascript", "java"}
        and scenario.variants
        for scenario in catalog.scenarios
    )

    catalog.validate_all_variant_sources()


def test_catalog_rejects_duplicate_scenario_slug(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    manifest = _json(root / "catalog.json")
    manifest["scenarios"][1]["slug"] = manifest["scenarios"][0]["slug"]
    _write_json(root / "catalog.json", manifest)

    with pytest.raises(CatalogValidationError, match="duplicate slug"):
        TemplateCatalog(root)


def test_catalog_accepts_supported_language_subset_but_rejects_empty_variants(
    tmp_path: Path,
) -> None:
    root = _copy_catalog(tmp_path)
    path, metadata = _first_metadata(root)
    metadata["variants"] = metadata["variants"][:1]
    _write_json(path, metadata)
    assert len(TemplateCatalog(root).get_scenario(metadata["slug"]).variants) == 1
    metadata["variants"] = []
    _write_json(path, metadata)
    with pytest.raises(CatalogValidationError):
        TemplateCatalog(root)


def test_catalog_rejects_unknown_enum_logo_and_unsafe_source_url(tmp_path: Path) -> None:
    enum_root = _copy_catalog(tmp_path / "enum")
    enum_path, enum_metadata = _first_metadata(enum_root)
    enum_metadata["variants"][0]["language"] = "ruby"
    _write_json(enum_path, enum_metadata)
    with pytest.raises(CatalogValidationError, match="ValidationError"):
        TemplateCatalog(enum_root)

    logo_root = _copy_catalog(tmp_path / "logo")
    logo_path, logo_metadata = _first_metadata(logo_root)
    logo_metadata["logo_key"] = "remote-vendor-logo"
    _write_json(logo_path, logo_metadata)
    with pytest.raises(CatalogValidationError, match="logo_key"):
        TemplateCatalog(logo_root)

    url_root = _copy_catalog(tmp_path / "url")
    provenance = _json(url_root / "provenance.json")
    provenance["sources"][0]["url"] = "javascript:alert(1)"
    _write_json(url_root / "provenance.json", provenance)
    with pytest.raises(CatalogValidationError, match="ValidationError"):
        TemplateCatalog(url_root)


@pytest.mark.parametrize(
    ("source_id", "forbidden_use_mode"),
    [
        ("open-c3-039b9a4", "adaptation-allowed"),
        ("alicloud-openapi-2026-09-05", "adaptation-allowed"),
        ("http-rfc9110-9112", "behavior-research-only"),
    ],
)
def test_catalog_rejects_incompatible_license_use_mode_combinations(
    tmp_path: Path,
    source_id: str,
    forbidden_use_mode: str,
) -> None:
    root = _copy_catalog(tmp_path)
    provenance = _json(root / "provenance.json")
    source = next(item for item in provenance["sources"] if item["id"] == source_id)
    source["use_mode"] = forbidden_use_mode
    _write_json(root / "provenance.json", provenance)

    with pytest.raises(CatalogValidationError, match="incompatible license/use_mode"):
        TemplateCatalog(root)


def test_catalog_rejects_tampered_official_source_license(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    provenance = _json(root / "provenance.json")
    source = next(
        item for item in provenance["sources"] if item["id"] == "alicloud-openapi-2026-09-05"
    )
    source["license"] = "GPL-2.0"
    _write_json(root / "provenance.json", provenance)

    with pytest.raises(CatalogValidationError, match="unexpected license"):
        TemplateCatalog(root)


def test_catalog_rejects_invalid_cross_reference_and_path_traversal(tmp_path: Path) -> None:
    cross_root = _copy_catalog(tmp_path / "cross")
    manifest = _json(cross_root / "catalog.json")
    manifest["scenarios"][0]["theme_slug"] = "missing-theme"
    _write_json(cross_root / "catalog.json", manifest)
    with pytest.raises(CatalogValidationError, match="unknown Theme"):
        TemplateCatalog(cross_root)

    source_root = _copy_catalog(tmp_path / "source")
    source_metadata_path, source_metadata = _first_metadata(source_root)
    provenance = _json(source_root / "provenance.json")
    foreign_source = next(
        item["id"]
        for item in provenance["sources"]
        if item["id"] not in source_metadata["provenance_ids"]
    )
    source_metadata["variants"][0]["provenance_ids"] = [foreign_source]
    _write_json(source_metadata_path, source_metadata)
    with pytest.raises(CatalogValidationError, match="included in its Scenario provenance"):
        TemplateCatalog(source_root)

    path_root = _copy_catalog(tmp_path / "path")
    metadata_path, metadata = _first_metadata(path_root)
    metadata["variants"][0]["code_resource"] = "variants/../../provenance.json"
    _write_json(metadata_path, metadata)
    with pytest.raises(CatalogValidationError, match="allowlisted catalog layout"):
        TemplateCatalog(path_root)


def test_variant_hash_is_checked_only_when_selected(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    metadata_path, metadata = _first_metadata(root)
    variant = metadata["variants"][0]
    variant["code_sha256"] = "0" * 64
    _write_json(metadata_path, metadata)

    catalog = TemplateCatalog(root)
    with pytest.raises(CatalogValidationError, match="SHA-256 mismatch"):
        catalog.load_variant(metadata["slug"], variant["language"])


def test_variant_cache_evicts_old_entries_instead_of_growing_without_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = TemplateCatalog(resources.files("dlr.control.template_catalog"))
    calls: list[str] = []
    original = catalog._read_variant_bytes

    def recording_read(resource_name: str) -> bytes:
        calls.append(resource_name)
        return original(resource_name)

    monkeypatch.setattr(catalog, "_read_variant_bytes", recording_read)
    catalog.validate_all_variant_sources()
    first = sorted(catalog.scenarios, key=lambda item: item.slug)[0]
    first_resource = f"variants/{first.slug}/python.py"
    assert calls.count(first_resource) == 1

    catalog.load_variant(first.slug, "python")
    assert calls.count(first_resource) == 2


def test_list_and_detail_do_not_read_code_but_variant_is_lazy_and_cached(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original = Path.read_bytes

    def recording_read(path: Path) -> bytes:
        normalized = path.as_posix()
        if "/variants/" in normalized:
            calls.append(normalized.split("/template_catalog/", maxsplit=1)[1])
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", recording_read)
    catalog = TemplateCatalog(resources.files("dlr.control.template_catalog"))
    assert calls == []
    monkeypatch.setattr(template_service, "get_template_catalog", lambda: catalog)
    scenario = catalog.scenarios[0]

    listed = api_client.get(
        "/api/templates/scenarios",
        params={"theme": scenario.theme_slug},
    )
    assert listed.status_code == 200, listed.text
    detail = api_client.get(f"/api/templates/scenarios/{scenario.slug}")
    assert detail.status_code == 200, detail.text
    assert calls == []

    selected = api_client.get(f"/api/templates/scenarios/{scenario.slug}/variants/javascript")
    assert selected.status_code == 200, selected.text
    assert calls == [f"variants/{scenario.slug}/javascript.mjs"]
    assert selected.json()["language"] == "javascript"
    assert "python" not in selected.json()

    again = api_client.get(f"/api/templates/scenarios/{scenario.slug}/variants/javascript")
    assert again.status_code == 200
    assert calls == [f"variants/{scenario.slug}/javascript.mjs"]


def test_theme_api_requires_auth_and_returns_fixed_bilingual_summary(
    api_client: TestClient,
) -> None:
    anonymous = TestClient(api_client.app)
    assert anonymous.get("/api/templates/themes").status_code == 401

    response = api_client.get("/api/templates/themes")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body) == 5
    assert [item["sort_order"] for item in body] == sorted(item["sort_order"] for item in body)
    assert sum(item["scenario_count"] for item in body) == 17
    assert all(item["name"]["zh-CN"] and item["name"]["en"] for item in body)
    serialized = json.dumps(body)
    assert '"code"' not in serialized
    assert "runtime_worker_id" not in serialized


def test_scenario_list_filters_with_and_semantics_and_stable_pagination(
    api_client: TestClient,
) -> None:
    themes = api_client.get("/api/templates/themes").json()
    populated_theme = max(themes, key=lambda item: item["scenario_count"])["slug"]
    first_response = api_client.get("/api/templates/scenarios", params={"theme": populated_theme})
    assert first_response.status_code == 200, first_response.text
    first_page = first_response.json()
    assert first_page["page"] == 1
    assert first_page["page_size"] == 12
    assert len(first_page["items"]) <= 12
    assert first_page["total"] >= len(first_page["items"])
    assert '"code"' not in json.dumps(first_page)
    catalog = template_service.get_template_catalog()
    expected_order = sorted(
        (item for item in catalog.scenarios if item.theme_slug == populated_theme),
        key=lambda item: (item.featured_rank, -item.updated_at.toordinal(), item.slug),
    )
    assert [item["slug"] for item in first_page["items"]] == [
        item.slug for item in expected_order[:12]
    ]

    item = first_page["items"][0]
    variant = item["variants"][0]
    params = {
        "theme": populated_theme,
        "q": item["vendor"],
        "vendor": item["vendor"],
        "adapter_type": item["adapter_type"],
        "protocol": item["protocols"][0],
        "language": variant["language"],
        "page_size": 1,
    }
    filtered = api_client.get("/api/templates/scenarios", params=params)
    assert filtered.status_code == 200, filtered.text
    result = filtered.json()
    assert result["total"] >= 1
    assert result["items"][0]["theme_slug"] == populated_theme
    assert result["items"][0]["vendor"] == item["vendor"]

    repeated = api_client.get("/api/templates/scenarios", params=params)
    assert repeated.json() == result
    empty = api_client.get(
        "/api/templates/scenarios",
        params={"theme": populated_theme, "q": "definitely-no-such-template-keyword"},
    )
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert empty.json()["total"] == 0
    past_end = api_client.get(
        "/api/templates/scenarios",
        params={"theme": populated_theme, "page": 999},
    )
    assert past_end.status_code == 200
    assert past_end.json()["page"] == 999
    assert past_end.json()["items"] == []
    assert past_end.json()["total"] == first_page["total"]


def test_all_templates_search_and_language_filter(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    slug = "rest-single-request"
    metadata_path = root / "scenarios" / slug / "metadata.json"
    metadata = _json(metadata_path)
    metadata["variants"] = [
        item for item in metadata["variants"] if item["language"] == "javascript"
    ]
    _write_json(metadata_path, metadata)

    catalog = TemplateCatalog(root)
    wrong_language = template_service.list_template_scenarios(
        language="python",
        q=metadata["title"]["en"],
        catalog=catalog,
    )
    matching_language = template_service.list_template_scenarios(
        language="javascript",
        q=metadata["title"]["en"],
        catalog=catalog,
    )
    assert slug not in {item.slug for item in wrong_language.items}
    assert slug in {item.slug for item in matching_language.items}
    assert template_service.list_template_scenarios(catalog=catalog).total == 17


@pytest.mark.parametrize(
    ("params", "expected_status", "expected_code"),
    [
        ({"theme": "missing"}, 422, "template_filter_invalid"),
        (
            {"theme": "cloud-cmdb", "vendor": "unknown-vendor"},
            422,
            "template_filter_invalid",
        ),
        (
            {"theme": "cloud-cmdb", "adapter_type": "stream"},
            422,
            "template_filter_invalid",
        ),
        (
            {"theme": "cloud-cmdb", "protocol": "gopher"},
            422,
            "template_filter_invalid",
        ),
        (
            {"theme": "cloud-cmdb", "language": "ruby"},
            422,
            "template_filter_invalid",
        ),
        ({"theme": "cloud-cmdb", "page": 0}, 422, None),
        ({"theme": "cloud-cmdb", "page_size": 49}, 422, None),
    ],
)
def test_scenario_list_rejects_invalid_queries(
    api_client: TestClient,
    params: dict[str, object],
    expected_status: int,
    expected_code: str | None,
) -> None:
    response = api_client.get("/api/templates/scenarios", params=params)
    assert response.status_code == expected_status
    if expected_code is not None:
        assert response.json()["detail"]["code"] == expected_code


def test_detail_and_variant_not_found_errors_are_stable(api_client: TestClient) -> None:
    missing = api_client.get("/api/templates/scenarios/not-a-template")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "template_scenario_not_found"

    language = api_client.get("/api/templates/scenarios/rest-single-request/variants/ruby")
    assert language.status_code == 404
    assert language.json()["detail"]["code"] == "template_variant_not_found"

    detail = api_client.get("/api/templates/scenarios/rest-single-request")
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    assert '"code"' not in json.dumps(detail_body)
    assert len(detail_body["variants"]) == 3
    assert "sources" not in detail_body
    assert all("maturity" not in variant for variant in detail_body["variants"])
    assert not {"input", "output_summary", "risk", "modes"} & detail_body.keys()
    for selected_language in ("python", "javascript", "java"):
        selected = api_client.get(
            f"/api/templates/scenarios/rest-single-request/variants/{selected_language}"
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["language"] == selected_language
        assert selected.json()["template_version"] == detail_body["template_version"]
        assert (
            not {
                "maturity",
                "receipt",
                "sources",
                "input_contract",
                "output_contract",
                "runtime_guidance",
                "install_notes",
                "behavior_contract_version",
            }
            & selected.json().keys()
        )


def test_traversal_like_identifiers_never_read_variant_resources(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = TemplateCatalog(resources.files("dlr.control.template_catalog"))
    calls: list[str] = []
    monkeypatch.setattr(
        catalog,
        "_read_variant_bytes",
        lambda value: calls.append(value) or b"should-not-be-read",
    )
    monkeypatch.setattr(template_service, "get_template_catalog", lambda: catalog)

    response = api_client.get(
        "/api/templates/scenarios/rest-single-request/variants/..%2F..%2Fcatalog.json"
    )
    assert response.status_code in {404, 422}
    assert calls == []


def test_invalid_filter_response_does_not_reflect_markup_or_remote_asset_input(
    api_client: TestClient,
) -> None:
    malicious = "<svg onload=alert(1)>https://evil.example/logo.svg"
    response = api_client.get(
        "/api/templates/scenarios",
        params={"theme": "cloud-cmdb", "vendor": malicious},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "template_filter_invalid",
        "message": "Template filter is invalid",
        "params": {"field": "vendor"},
    }
    assert malicious not in response.text


def test_request_validation_errors_never_reflect_rejected_template_values(
    api_client: TestClient,
) -> None:
    canary = "ISSUE132_DO_NOT_REFLECT_SECRET_CANARY"
    expected_detail = {
        "code": "template_request_invalid",
        "message": "Template request is invalid",
    }

    query_response = api_client.get(
        "/api/templates/scenarios",
        params={"theme": "cloud-cmdb", "q": canary * 8},
    )
    assert query_response.status_code == 422
    assert query_response.json()["detail"] == expected_detail
    assert canary not in query_response.text

    body_response = api_client.post(
        "/api/templates/scenarios/rest-single-request/variants/python/instantiate",
        json={
            "name": "validation-never-reaches-service",
            "expected_template_version": "1.0.0",
            "credential": canary,
        },
    )
    assert body_response.status_code == 422
    assert body_response.json()["detail"] == expected_detail
    assert canary not in body_response.text


def test_file_template_browsing_does_not_depend_on_managed_input_store(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "managed_files_enabled", False)
    for slug in ("csv-to-json", "excel-to-json"):
        detail = api_client.get(f"/api/templates/scenarios/{slug}")
        variant = api_client.get(f"/api/templates/scenarios/{slug}/variants/python")
        assert detail.status_code == 200, detail.text
        assert variant.status_code == 200, variant.text
        assert variant.json()["scenario_slug"] == slug


def test_built_wheel_contains_and_loads_every_catalog_resource(tmp_path: Path) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    output = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output)],
        cwd=backend_root,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(output.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        root = zipfile.Path(archive, "dlr/control/template_catalog/")
        catalog = TemplateCatalog(root)
        catalog.validate_all_variant_sources()
        expected_variants = sum(len(scenario.variants) for scenario in catalog.scenarios)
        assert expected_variants >= len(catalog.scenarios)

    installed = tmp_path / "installed"
    subprocess.run(
        ["uv", "pip", "install", "--target", str(installed), "--no-deps", str(wheel)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(installed)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "from importlib import resources",
                    "from pathlib import Path",
                    "import dlr",
                    "from dlr.control.template_catalog import TemplateCatalog",
                    f"installed = Path({str(installed)!r}).resolve()",
                    "assert Path(dlr.__file__).resolve().is_relative_to(installed)",
                    "catalog = TemplateCatalog(resources.files('dlr.control.template_catalog'))",
                    "catalog.validate_all_variant_sources()",
                    "print(sum(len(item.variants) for item in catalog.scenarios))",
                )
            ),
        ],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == str(expected_variants)
