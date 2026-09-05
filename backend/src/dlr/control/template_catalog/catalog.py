"""Validated, lazy reader for the shipped Template Gallery catalog."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath
from types import MappingProxyType

from pydantic import BaseModel, ValidationError

from dlr.control.template_catalog.models import (
    TemplateCatalogManifest,
    TemplateProvenanceCatalog,
    TemplateProvenanceSource,
    TemplateScenarioAsset,
    TemplateThemeAsset,
    TemplateVariantAsset,
)

RESOURCE_PACKAGE = "dlr.control.template_catalog"
MAX_VARIANT_BYTES = 1024 * 1024
VARIANT_CACHE_SIZE = 32

EXPECTED_THEME_SLUGS = frozenset(
    {"cloud-cmdb", "api-events", "file-data", "databases", "storage-transfer"}
)
EXPECTED_SCENARIO_SLUGS = frozenset(
    {
        "alicloud-compute-container-topology",
        "alicloud-network-ingress-topology",
        "alicloud-database-middleware-inventory",
        "tencentcloud-compute-container-topology",
        "tencentcloud-network-ingress-topology",
        "tencentcloud-database-middleware-inventory",
        "servicenow-cmdb-ci-snapshot",
        "rest-single-request",
        "rest-paginated-collection",
        "webhook-json-normalization",
        "csv-to-json",
        "excel-to-json",
        "json-mapping-cleaning",
        "postgresql-readonly-snapshot",
        "mysql-readonly-snapshot",
        "s3-compatible-list-read",
        "sftp-list-read",
    }
)
EXPECTED_LOGO_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "alicloud-compute-container-topology": "alicloud-compute",
        "alicloud-network-ingress-topology": "alicloud-network",
        "alicloud-database-middleware-inventory": "alicloud-data",
        "tencentcloud-compute-container-topology": "tencentcloud-compute",
        "tencentcloud-network-ingress-topology": "tencentcloud-network",
        "tencentcloud-database-middleware-inventory": "tencentcloud-data",
        "servicenow-cmdb-ci-snapshot": "servicenow-cmdb",
        "rest-single-request": "rest-request",
        "rest-paginated-collection": "rest-pagination",
        "webhook-json-normalization": "webhook-normalize",
        "csv-to-json": "file-csv",
        "excel-to-json": "file-excel",
        "json-mapping-cleaning": "data-json",
        "postgresql-readonly-snapshot": "database-postgresql",
        "mysql-readonly-snapshot": "database-mysql",
        "s3-compatible-list-read": "storage-s3",
        "sftp-list-read": "transfer-sftp",
    }
)
EXPECTED_LANGUAGES = frozenset({"python", "javascript", "java"})
OFFICIAL_API_SOURCE_LICENSES: Mapping[str, str] = MappingProxyType(
    {
        "alicloud-openapi-2026-09-05": "Apache-2.0",
        "tencentcloud-api-2026-09-05": "Tencent Cloud documentation terms",
        "servicenow-table-api-2026-09-05": "ServiceNow documentation terms",
        "http-rfc9110-9112": "IETF Trust Legal Provisions",
        "csv-rfc4180": "IETF Trust Legal Provisions",
        "excel-formats-and-libraries-2026-09-05": (
            "ECMA copyright for the standard; libraries use their respective published licenses"
        ),
        "json-pointer-rfc6901": "IETF Trust Legal Provisions",
        "postgresql-17-docs": "PostgreSQL documentation license",
        "mysql-9-docs": "Oracle MySQL documentation terms",
        "s3-api-2006-03-01": "AWS documentation terms",
        "sftp-v3-openssh": ("IETF Trust Legal Provisions; libraries use their published licenses"),
    }
)
EXPECTED_LANGUAGE_FILENAMES: Mapping[str, str] = MappingProxyType(
    {"python": "python.py", "javascript": "javascript.mjs", "java": "java.java"}
)
LICENSE_USE_MODE_POLICY: Mapping[str, str] = MappingProxyType(
    {
        "GPL-2.0": "behavior-research-only",
        "GPL-3.0": "behavior-research-only",
        "Elastic License 2.0": "behavior-research-only",
        "No repository license found": "behavior-research-only",
        "Apache-2.0": "adaptation-allowed",
        "Apache-2.0 (repository LICENSE); GitHub API reported NOASSERTION": ("adaptation-allowed"),
        "Tencent Cloud documentation terms": "official-api",
        "ServiceNow documentation terms": "official-api",
        "IETF Trust Legal Provisions": "official-api",
        (
            "ECMA copyright for the standard; libraries use their respective published licenses"
        ): "official-api",
        "PostgreSQL documentation license": "official-api",
        "Oracle MySQL documentation terms": "official-api",
        "AWS documentation terms": "official-api",
        ("IETF Trust Legal Provisions; libraries use their published licenses"): "official-api",
    }
)


class CatalogValidationError(RuntimeError):
    """The shipped catalog is incomplete, inconsistent or tampered with."""


@dataclass(frozen=True)
class LoadedTemplateVariant:
    """One selected Variant plus its verified UTF-8 source text."""

    scenario: TemplateScenarioAsset
    variant: TemplateVariantAsset
    code: str
    sources: tuple[TemplateProvenanceSource, ...]


def _stable_error(resource: str, reason: str) -> CatalogValidationError:
    return CatalogValidationError(f"invalid template asset {resource}: {reason}")


def _safe_resource_path(value: str, *, prefix: str, suffix: str) -> PurePosixPath:
    if "\\" in value:
        raise _stable_error(value, "resource paths must use POSIX separators")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or path.as_posix() != value
        or not value.startswith(prefix)
        or not value.endswith(suffix)
    ):
        raise _stable_error(value, "resource path is outside the allowlisted catalog layout")
    return path


def _resource_at(root: Traversable, value: str) -> Traversable:
    path = PurePosixPath(value)
    resource = root.joinpath(*path.parts)
    if not resource.is_file():
        raise _stable_error(value, "resource is missing")
    return resource


def _read_json[AssetModel: BaseModel](
    root: Traversable, value: str, model_type: type[AssetModel]
) -> AssetModel:
    resource = _resource_at(root, value)
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
        return model_type.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise _stable_error(value, type(exc).__name__) from exc


def _unique_map[AssetModel: BaseModel](
    items: tuple[AssetModel, ...], field: str, *, resource: str
) -> dict[str, AssetModel]:
    result: dict[str, AssetModel] = {}
    for item in items:
        key = getattr(item, field)
        if not isinstance(key, str):  # pragma: no cover - internal schema misuse
            raise _stable_error(resource, f"{field} must be a string")
        if key in result:
            raise _stable_error(resource, f"duplicate {field} {key}")
        result[key] = item
    return result


class TemplateCatalog:
    """Immutable catalog metadata with a bounded selected-source cache.

    Construction reads the manifest, Scenario metadata and provenance,
    but deliberately does not read any Variant source.
    ``load_variant`` is the only runtime path that reads code. Release gates
    call ``validate_all_variant_sources`` to verify all declared hashes.
    """

    def __init__(self, root: Traversable) -> None:
        self._root = root
        self._variant_cache: OrderedDict[tuple[str, str, str], LoadedTemplateVariant] = (
            OrderedDict()
        )
        self._variant_cache_lock = threading.RLock()

        manifest = _read_json(root, "catalog.json", TemplateCatalogManifest)
        provenance = _read_json(root, "provenance.json", TemplateProvenanceCatalog)
        self.manifest = manifest
        self.provenance = provenance
        self._validate_and_index()

    def _validate_and_index(self) -> None:
        themes = _unique_map(self.manifest.themes, "slug", resource="catalog.json")
        if frozenset(themes) != EXPECTED_THEME_SLUGS:
            raise _stable_error(
                "catalog.json", "theme inventory must be exactly 5 published themes"
            )
        sort_orders = [theme.sort_order for theme in themes.values()]
        if len(sort_orders) != len(set(sort_orders)):
            raise _stable_error("catalog.json", "theme sort_order values must be unique")

        refs = _unique_map(self.manifest.scenarios, "slug", resource="catalog.json")
        if frozenset(refs) != EXPECTED_SCENARIO_SLUGS:
            raise _stable_error(
                "catalog.json", "Scenario inventory must be exactly 17 published scenarios"
            )

        sources = _unique_map(self.provenance.sources, "id", resource="provenance.json")
        for source in sources.values():
            official_license = OFFICIAL_API_SOURCE_LICENSES.get(source.id)
            if official_license is not None and source.license != official_license:
                raise _stable_error(
                    "provenance.json",
                    f"official source {source.id} has an unexpected license",
                )
            expected_use_mode = (
                "official-api"
                if official_license is not None
                else LICENSE_USE_MODE_POLICY.get(source.license)
            )
            if expected_use_mode is None:
                raise _stable_error(
                    "provenance.json",
                    f"source {source.id} has an unclassified license",
                )
            if source.use_mode != expected_use_mode:
                raise _stable_error(
                    "provenance.json",
                    f"source {source.id} has an incompatible license/use_mode combination",
                )
        scenarios: dict[str, TemplateScenarioAsset] = {}
        variants: dict[tuple[str, str], TemplateVariantAsset] = {}

        for slug, ref in refs.items():
            theme_slug = ref.theme_slug
            metadata_resource = ref.metadata_resource
            if theme_slug not in themes:
                raise _stable_error("catalog.json", f"unknown Theme {theme_slug} for {slug}")
            expected_metadata = f"scenarios/{slug}/metadata.json"
            _safe_resource_path(metadata_resource, prefix="scenarios/", suffix="/metadata.json")
            if metadata_resource != expected_metadata:
                raise _stable_error(metadata_resource, f"unexpected mapping for {slug}")
            scenario = _read_json(self._root, metadata_resource, TemplateScenarioAsset)
            if scenario.slug != slug or scenario.theme_slug != theme_slug:
                raise _stable_error(metadata_resource, "Scenario identity does not match catalog")
            if scenario.logo_key != EXPECTED_LOGO_KEYS[slug]:
                raise _stable_error(metadata_resource, f"unknown or mismatched logo_key for {slug}")
            language_map = _unique_map(scenario.variants, "language", resource=metadata_resource)
            if not language_map or not frozenset(language_map).issubset(EXPECTED_LANGUAGES):
                raise _stable_error(metadata_resource, "Scenario must contain supported languages")
            self._validate_provenance_ids(
                scenario.provenance_ids, sources, resource=metadata_resource
            )

            for language, variant in language_map.items():
                if variant.behavior_contract_version != self.manifest.behavior_contract_version:
                    raise _stable_error(
                        metadata_resource, "Variant behavior contract does not match catalog"
                    )
                expected_code = f"variants/{slug}/{EXPECTED_LANGUAGE_FILENAMES[language]}"
                _safe_resource_path(
                    variant.code_resource,
                    prefix="variants/",
                    suffix=PurePosixPath(expected_code).suffix,
                )
                if variant.code_resource != expected_code:
                    raise _stable_error(variant.code_resource, "unexpected Variant source mapping")
                _resource_at(self._root, variant.code_resource)
                self._validate_provenance_ids(
                    variant.provenance_ids, sources, resource=metadata_resource
                )
                if not set(variant.provenance_ids).issubset(scenario.provenance_ids):
                    raise _stable_error(
                        metadata_resource,
                        "Variant provenance must be included in its Scenario provenance",
                    )
                key = (slug, language)
                variants[key] = variant
            scenarios[slug] = scenario

        coverage_scenarios = {entry.scenario_slug for entry in self.provenance.coverage}
        unknown_coverage = coverage_scenarios - EXPECTED_SCENARIO_SLUGS
        missing_coverage = EXPECTED_SCENARIO_SLUGS - coverage_scenarios
        if unknown_coverage or missing_coverage:
            raise _stable_error(
                "provenance.json",
                "coverage must reference every published Scenario and no unknown Scenario",
            )
        coverage_keys: set[tuple[str, str]] = set()
        for entry in self.provenance.coverage:
            self._validate_provenance_ids(entry.provenance_ids, sources, resource="provenance.json")
            key = (entry.scenario_slug, entry.resource_family)
            if key in coverage_keys:
                raise _stable_error("provenance.json", "duplicate Scenario resource coverage")
            coverage_keys.add(key)
            if not set(entry.provenance_ids).issubset(
                scenarios[entry.scenario_slug].provenance_ids
            ):
                raise _stable_error(
                    "provenance.json",
                    "coverage provenance must be included in its Scenario provenance",
                )

        self._themes = MappingProxyType(themes)
        self._scenarios = MappingProxyType(scenarios)
        self._variants = MappingProxyType(variants)
        self._sources = MappingProxyType(sources)

    @staticmethod
    def _validate_provenance_ids(
        identifiers: tuple[str, ...],
        sources: Mapping[str, TemplateProvenanceSource],
        *,
        resource: str,
    ) -> None:
        if len(identifiers) != len(set(identifiers)):
            raise _stable_error(resource, "duplicate provenance id")
        unknown = set(identifiers) - set(sources)
        if unknown:
            raise _stable_error(resource, f"unknown provenance id {sorted(unknown)[0]}")

    @property
    def themes(self) -> tuple[TemplateThemeAsset, ...]:
        return tuple(sorted(self._themes.values(), key=lambda item: (item.sort_order, item.slug)))

    @property
    def scenarios(self) -> tuple[TemplateScenarioAsset, ...]:
        return tuple(self._scenarios.values())

    @property
    def vendors(self) -> frozenset[str]:
        return frozenset(scenario.vendor for scenario in self._scenarios.values())

    @property
    def protocols(self) -> frozenset[str]:
        return frozenset(
            protocol for scenario in self._scenarios.values() for protocol in scenario.protocols
        )

    def get_theme(self, slug: str) -> TemplateThemeAsset | None:
        return self._themes.get(slug)

    def get_scenario(self, slug: str) -> TemplateScenarioAsset | None:
        return self._scenarios.get(slug)

    def get_variant(self, slug: str, language: str) -> TemplateVariantAsset | None:
        return self._variants.get((slug, language))

    def sources_for(self, identifiers: tuple[str, ...]) -> tuple[TemplateProvenanceSource, ...]:
        return tuple(self._sources[identifier] for identifier in identifiers)

    def load_variant(self, slug: str, language: str) -> LoadedTemplateVariant | None:
        scenario = self.get_scenario(slug)
        variant = self.get_variant(slug, language)
        if scenario is None or variant is None:
            return None
        cache_key = (slug, scenario.version, language)
        with self._variant_cache_lock:
            cached = self._variant_cache.get(cache_key)
            if cached is not None:
                self._variant_cache.move_to_end(cache_key)
                return cached

        try:
            code_bytes = self._read_variant_bytes(variant.code_resource)
        except OSError as exc:
            raise _stable_error(variant.code_resource, type(exc).__name__) from exc
        digest = hashlib.sha256(code_bytes).hexdigest()
        if digest != variant.code_sha256:
            raise _stable_error(variant.code_resource, "content SHA-256 mismatch")
        if not code_bytes or len(code_bytes) > MAX_VARIANT_BYTES:
            raise _stable_error(variant.code_resource, "Variant source size is invalid")
        try:
            code = code_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _stable_error(variant.code_resource, "Variant source is not UTF-8") from exc
        if not code.strip():
            raise _stable_error(variant.code_resource, "Variant source is blank")
        loaded = LoadedTemplateVariant(
            scenario=scenario,
            variant=variant,
            code=code,
            sources=self.sources_for(variant.provenance_ids),
        )
        with self._variant_cache_lock:
            self._variant_cache[cache_key] = loaded
            self._variant_cache.move_to_end(cache_key)
            while len(self._variant_cache) > VARIANT_CACHE_SIZE:
                self._variant_cache.popitem(last=False)
        return loaded

    def _read_variant_bytes(self, resource_name: str) -> bytes:
        return _resource_at(self._root, resource_name).read_bytes()

    def clear_variant_cache(self) -> None:
        with self._variant_cache_lock:
            self._variant_cache.clear()

    def validate_all_variant_sources(self) -> None:
        """Read and hash every source for build, wheel and image gates."""
        for scenario in sorted(self._scenarios.values(), key=lambda item: item.slug):
            for variant in scenario.variants:
                if self.load_variant(scenario.slug, variant.language) is None:  # pragma: no cover
                    raise _stable_error(
                        f"{scenario.slug}/{variant.language}",
                        "validated Variant unexpectedly missing",
                    )


@lru_cache(maxsize=1)
def get_template_catalog() -> TemplateCatalog:
    """Return the validated process-local catalog metadata singleton."""
    return TemplateCatalog(resources.files(RESOURCE_PACKAGE))


def reset_template_catalog_cache() -> None:
    """Clear catalog metadata/source caches for tests and controlled reloads."""
    catalog = get_template_catalog.cache_info()
    if catalog.currsize:
        get_template_catalog().clear_variant_cache()
    get_template_catalog.cache_clear()


def validate_template_catalog_assets() -> TemplateCatalog:
    """Fail closed after validating metadata and all 51 source hashes."""
    catalog = get_template_catalog()
    catalog.validate_all_variant_sources()
    return catalog
