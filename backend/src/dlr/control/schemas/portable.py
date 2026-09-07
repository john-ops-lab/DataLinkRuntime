"""Versioned, allowlisted portable content. Deployment identities are never fields."""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dlr.control.schemas.adapter import _validate_name
from dlr.control.services.schedule import validate_cron, validate_timezone


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PortableSchedule(StrictModel):
    cron: str
    timezone: str
    misfire_policy: Literal["coalesce_latest", "queue_every_occurrence", "skip_while_busy"] = (
        "coalesce_latest"
    )
    max_catchup_count: int = Field(default=100, ge=1, le=1000)
    max_catchup_age_seconds: int = Field(default=86400, ge=60, le=604800)

    _cron = field_validator("cron")(validate_cron)
    _timezone = field_validator("timezone")(validate_timezone)


class PortableWebhook(StrictModel):
    response_mode: Literal["accepted", "completed"] = "accepted"
    response_timeout_seconds: int = Field(default=30, ge=1, le=300)


class PortableVariant(StrictModel):
    language: Literal["python", "javascript", "java"]
    code: str = Field(min_length=1, max_length=1048576)
    requirements: str = Field(default="", max_length=1048576)
    runtime_config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_code(self) -> "PortableVariant":
        if not self.code.strip():
            raise ValueError("code must not be blank")
        return self


class PortableFile(StrictModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=255)
    data_base64: str = Field(max_length=24 * 1024 * 1024)


class PortableInput(StrictModel):
    source_type: Literal["none", "json", "managed_files", "remote_files"] = "none"
    included: bool = False
    json_value: Any = None
    files: list[PortableFile] = Field(default_factory=list, max_length=9)

    @model_validator(mode="after")
    def validate_content(self) -> "PortableInput":
        if self.files and (self.source_type != "managed_files" or not self.included):
            raise ValueError("files require explicit managed input inclusion")
        if self.json_value is not None and (self.source_type != "json" or not self.included):
            raise ValueError("JSON requires explicit JSON input inclusion")
        if self.included and self.source_type not in {"json", "managed_files"}:
            raise ValueError("unsupported input inclusion")
        return self


class PortablePackage(StrictModel):
    format_version: Literal[1] = 1
    object_type: Literal["adapter", "template"]
    name: str
    description: str = Field(default="", max_length=20000)
    category: str = Field(default="other", max_length=128)
    variants: list[PortableVariant] = Field(min_length=1, max_length=3)
    timeout_seconds: int = Field(default=300, ge=1, le=86400)
    adapter_type: Literal["task", "webhook"]
    schedule: PortableSchedule | None = None
    webhook: PortableWebhook | None = None
    input: PortableInput = Field(default_factory=PortableInput)
    provenance: str = Field(default="", max_length=100000)
    license: str = Field(default="", max_length=100000)

    _name = field_validator("name", mode="before")(_validate_name)

    @model_validator(mode="after")
    def validate_object(self) -> "PortablePackage":
        json.dumps(self.model_dump(), allow_nan=False)
        languages = [v.language for v in self.variants]
        if len(set(languages)) != len(languages):
            raise ValueError("duplicate language")
        if self.object_type == "adapter" and len(languages) != 1:
            raise ValueError("an adapter has one language")
        if self.object_type == "template" and self.input != PortableInput():
            raise ValueError("templates cannot bind production input")
        if self.adapter_type == "webhook" and (self.schedule or self.input != PortableInput()):
            raise ValueError("Webhook cannot carry task configuration")
        if self.adapter_type == "task" and self.webhook:
            raise ValueError("Task cannot carry Webhook configuration")
        return self


class AdapterExportOptions(StrictModel):
    include_json: bool = False
    include_files: bool = False
    as_template: bool = False


class PortableImportRequest(StrictModel):
    package: PortablePackage
    runtime_worker_id: int | None = Field(default=None, gt=0)


class TemplateWriteRequest(StrictModel):
    package: PortablePackage
    expected_version: str | None = None
