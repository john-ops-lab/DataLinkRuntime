"""Strict immutable schemas for the versioned Template Gallery assets."""

from datetime import date
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

TemplateLanguage = Literal["python", "javascript", "java"]
TemplateAdapterType = Literal["task", "webhook"]
TemplateSourceUseMode = Literal[
    "adaptation-allowed",
    "behavior-research-only",
    "official-api",
]
TemplateSupportStatus = Literal["supported", "gap"]

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
VersionText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
Slug = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class FrozenAssetModel(BaseModel):
    """Fail closed on unknown fields and prevent top-level mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class LocalizedText(FrozenAssetModel):
    """One catalog string with both supported display locales."""

    zh_cn: NonEmptyText = Field(alias="zh-CN")
    en: NonEmptyText

    @field_validator("zh_cn", "en")
    @classmethod
    def reject_markup(cls, value: str) -> str:
        # Catalog prose is rendered as text. Rejecting angle brackets here
        # prevents a future client from treating an asset as trusted HTML.
        if "<" in value or ">" in value:
            raise ValueError("catalog display text must not contain markup")
        return value


class TemplateThemeAsset(FrozenAssetModel):
    slug: Slug
    name: LocalizedText
    description: LocalizedText
    sort_order: int = Field(ge=0)


class TemplateScenarioReference(FrozenAssetModel):
    slug: Slug
    theme_slug: Slug
    metadata_resource: NonEmptyText


class TemplateCatalogManifest(FrozenAssetModel):
    catalog_version: VersionText
    behavior_contract_version: VersionText
    themes: tuple[TemplateThemeAsset, ...]
    scenarios: tuple[TemplateScenarioReference, ...]


class TemplateVariantAsset(FrozenAssetModel):
    language: TemplateLanguage
    behavior_contract_version: VersionText
    code_resource: NonEmptyText
    code_sha256: Sha256
    requirements: str
    input_skeleton: dict[str, object]
    output_example: dict[str, object]
    runtime_config: dict[str, object]
    provenance_ids: tuple[NonEmptyText, ...] = Field(min_length=1)


class TemplateScenarioAsset(FrozenAssetModel):
    slug: Slug
    theme_slug: Slug
    title: LocalizedText
    summary: LocalizedText
    details: LocalizedText
    vendor: NonEmptyText
    adapter_type: TemplateAdapterType
    protocols: tuple[NonEmptyText, ...] = Field(min_length=1)
    tags: tuple[NonEmptyText, ...] = Field(min_length=1)
    logo_key: NonEmptyText
    version: VersionText
    updated_at: date
    featured_rank: int = Field(ge=0)
    provenance_ids: tuple[NonEmptyText, ...] = Field(min_length=1)
    variants: tuple[TemplateVariantAsset, ...]


class TemplateSdkReferences(FrozenAssetModel):
    python: object | None = None
    javascript: object | None = None
    java: object | None = None


class TemplateProvenanceSource(FrozenAssetModel):
    id: NonEmptyText
    url: NonEmptyText
    revision: NonEmptyText
    reference: NonEmptyText
    license: NonEmptyText
    license_evidence: NonEmptyText
    use_mode: TemplateSourceUseMode
    pagination: NonEmptyText
    relationship_evidence: NonEmptyText
    sdk: TemplateSdkReferences
    fixture: NonEmptyText
    checked_at: date

    @field_validator("url")
    @classmethod
    def require_https_url_without_credentials(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("provenance URL must use HTTPS without credentials")
        return value


class TemplateCoverageEntry(FrozenAssetModel):
    scenario_slug: Slug
    resource_family: NonEmptyText
    provenance_ids: tuple[NonEmptyText, ...] = Field(min_length=1)
    api_operations: tuple[NonEmptyText, ...]
    pagination: NonEmptyText
    external_key: NonEmptyText
    relationships: tuple[NonEmptyText, ...]
    support_status: TemplateSupportStatus
    notes: LocalizedText


class TemplateProvenanceCatalog(FrozenAssetModel):
    schema_version: VersionText
    checked_at: date
    sources: tuple[TemplateProvenanceSource, ...]
    coverage: tuple[TemplateCoverageEntry, ...]
