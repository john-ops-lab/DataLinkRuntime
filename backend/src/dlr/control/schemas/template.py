"""Public request and response schemas for the Template Gallery."""

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from dlr.control.schemas.adapter import _validate_name

TemplateLanguage = Literal["python", "javascript", "java"]
TemplateVersionInput = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]


class TemplateLocalizedText(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    zh_cn: str = Field(alias="zh-CN")
    en: str


class TemplateThemeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: TemplateLocalizedText
    description: TemplateLocalizedText
    sort_order: int
    scenario_count: int


class TemplateVariantSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: TemplateLanguage
    available: bool = True


class TemplateScenarioSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    theme_slug: str
    title: TemplateLocalizedText
    summary: TemplateLocalizedText
    vendor: str
    adapter_type: Literal["task", "webhook"]
    protocols: list[str]
    tags: list[str]
    logo_key: str
    template_version: str
    updated_at: date
    variants: list[TemplateVariantSummary]


class TemplateScenarioListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[TemplateScenarioSummary]
    page: int
    page_size: int
    total: int


class TemplateScenarioDetail(TemplateScenarioSummary):
    details: TemplateLocalizedText


class TemplateVariantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_slug: str
    theme_slug: str
    title: TemplateLocalizedText
    language: TemplateLanguage
    adapter_type: Literal["task", "webhook"]
    template_version: str
    code: str
    requirements: str
    input_skeleton: dict[str, Any]
    output_example: dict[str, Any]
    runtime_config: dict[str, Any]


class TemplateInstantiateRequest(BaseModel):
    """Only user-owned metadata and the reviewed version are client-controlled."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    expected_template_version: TemplateVersionInput

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        return _validate_name(value)
