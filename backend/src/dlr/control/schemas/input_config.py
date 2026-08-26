"""Pydantic contracts for the Adapter-level input object API."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

InputSourceType = Literal["none", "json", "managed_files", "remote_files"]
InputRetentionMode = Literal["system_default", "custom", "manual_delete"]


class InputRetention(BaseModel):
    """Retention policy carried only by a managed-file configuration."""

    model_config = ConfigDict(extra="forbid")

    mode: InputRetentionMode
    seconds: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_seconds(self) -> "InputRetention":
        if self.mode == "custom" and self.seconds is None:
            raise ValueError("custom retention requires seconds")
        if self.mode != "custom" and self.seconds is not None:
            raise ValueError("seconds is only valid for custom retention")
        return self


class AdapterInputConfigUpsert(BaseModel):
    """PUT /api/adapters/{adapter_id}/input-config request."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(gt=0)
    source_type: InputSourceType
    # ``model_fields_set`` distinguishes omitted json_value from explicit
    # JSON null, which is a valid JSON source value.
    json_value: Any = None
    artifact_ids: list[int] | None = Field(default=None, max_length=8)
    retention: InputRetention | None = None

    @model_validator(mode="after")
    def validate_source_fields(self) -> "AdapterInputConfigUpsert":
        fields = self.model_fields_set
        if self.source_type == "none":
            forbidden = fields.intersection({"json_value", "artifact_ids", "retention"})
            if forbidden:
                raise ValueError("none input does not accept source-specific fields")
        elif self.source_type == "json":
            if "json_value" not in fields:
                raise ValueError("json input requires json_value, including explicit null")
            forbidden = fields.intersection({"artifact_ids", "retention"})
            if forbidden:
                raise ValueError("json input does not accept file or retention fields")
        elif self.source_type == "managed_files":
            if "artifact_ids" not in fields or self.artifact_ids is None:
                raise ValueError("managed_files input requires artifact_ids")
            if "retention" not in fields or self.retention is None:
                raise ValueError("managed_files input requires retention")
            if len(set(self.artifact_ids)) != len(self.artifact_ids):
                raise ValueError("artifact_ids must be unique")
            if any(artifact_id <= 0 for artifact_id in self.artifact_ids):
                raise ValueError("artifact_ids must be positive")
            if "json_value" in fields:
                raise ValueError("managed_files input does not accept json_value")
        else:
            forbidden = fields.intersection({"json_value", "artifact_ids", "retention"})
            if forbidden:
                raise ValueError("remote_files input does not accept source-specific fields")
        return self


class AdapterInputConfigResponse(BaseModel):
    """Safe current input representation returned by GET/PUT."""

    model_config = ConfigDict(extra="forbid")

    adapter_id: int
    revision: int
    source_type: InputSourceType
    json_value: Any
    retention: InputRetention
    # A0 has no Artifact table yet. Keeping the public field stable lets later
    # waves add safe metadata without exposing IDs, keys, paths, or content.
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    valid_for_run: bool
    invalid_reason: str | None = None
