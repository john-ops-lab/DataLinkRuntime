"""Closed subscription management contracts; source secrets are references only."""

from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class SourceModel(ClosedModel):
    @model_validator(mode="after")
    def persistable_json(self) -> "SourceModel":
        def check(value: Any) -> None:
            if isinstance(value, str):
                if "\x00" in value:
                    raise ValueError("Source configuration contains an unsupported character")
                value.encode("utf-8")
            elif isinstance(value, dict):
                for item in value.values():
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)

        try:
            check(self.model_dump())
        except UnicodeError:
            raise ValueError("Source configuration must be valid UTF-8") from None
        return self


class KafkaSourceConfig(SourceModel):
    source_type: Literal["kafka"] = "kafka"
    cluster_id: str = Field(min_length=1, max_length=256)
    bootstrap_servers: list[str] = Field(min_length=1, max_length=16)
    topics: list[str] = Field(min_length=1, max_length=16)
    group_id: str | None = Field(default=None, min_length=1, max_length=256)
    initial_position: Literal["earliest", "latest"]
    security_protocol: Literal["PLAINTEXT", "SSL", "SASL_SSL"] = "SSL"
    sasl_mechanism: Literal["SCRAM-SHA-256", "SCRAM-SHA-512"] | None = None
    credential_id: int | None = Field(default=None, gt=0)
    ca_pem: str | None = Field(default=None, max_length=65536)
    mode: Literal["per_event"] = "per_event"
    ordering: Literal["partition_serial"] = "partition_serial"
    limits: dict[str, StrictInt] = Field(default_factory=dict)

    @field_validator("bootstrap_servers", "topics")
    @classmethod
    def validate_names(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            not item or len(item) > 512 or any(c in item for c in "\x00\r\n/@?#") for item in value
        ):
            raise ValueError("Source names must be unique, bounded and contain no credentials")
        return value


class KubernetesSourceConfig(SourceModel):
    source_type: Literal["kubernetes"] = "kubernetes"
    cluster_id: str = Field(min_length=1, max_length=256)
    server: str = Field(max_length=2048)
    group: str = Field(default="", max_length=256)
    version: str = Field(min_length=1, max_length=128)
    resource: str = Field(min_length=1, max_length=128)
    namespace: str | None = Field(default=None, max_length=253)
    label_selector: str = Field(default="", max_length=4096)
    field_selector: str = Field(default="", max_length=4096)
    credential_id: int | None = Field(default=None, gt=0)
    ca_pem: str | None = Field(default=None, max_length=65536)
    mode: Literal["per_event", "state_sync"] = "per_event"
    limits: dict[str, StrictInt] = Field(default_factory=dict)

    @field_validator("server")
    @classmethod
    def validate_server(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Kubernetes server must be an HTTPS origin without credentials")
        return value.rstrip("/")


SourceConfig = Annotated[
    KafkaSourceConfig | KubernetesSourceConfig, Field(discriminator="source_type")
]


class SubscriptionCreate(ClosedModel):
    adapter_id: int = Field(gt=0)
    name: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=4096)
    config: SourceConfig


class SubscriptionUpdate(ClosedModel):
    expected_config_version: int | None = Field(default=None, ge=1, strict=True)
    name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=4096)
    config: SourceConfig | None = None
    new_epoch: bool = False


class SubscriptionAction(ClosedModel):
    expected_config_version: int = Field(ge=1, strict=True)


class SubscriptionDisposition(SubscriptionAction):
    request_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=4096)

    @field_validator("request_id", "reason")
    @classmethod
    def audit_text(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("An audit request ID and a meaningful reason are required")
        value.encode("utf-8")
        return value


class SubscriptionReplay(SubscriptionDisposition):
    revision_id: int | None = Field(default=None, gt=0)
    use_current_configuration: bool = False


class SubscriptionReset(SubscriptionDisposition):
    config: SourceConfig | None = None


class SubscriptionClone(SubscriptionAction):
    target_adapter_id: int = Field(gt=0)


class RepairRevision(SubscriptionAction):
    code: str = Field(min_length=1)
    requirements: str = ""

    @field_validator("code")
    @classmethod
    def nonblank_code(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Code cannot be blank")
        return value


class SubscriptionResponse(BaseModel):
    id: int
    adapter_id: int
    name: str
    description: str
    source_type: str
    desired_state: str
    connection_state: str
    delivery_state: str
    config_version: int
    source_epoch: str
    activation_id: int | None
    repair_revision_id: int | None
    last_error_code: str | None
    config: dict[str, Any]
    quota: dict[str, int]
    oldest_pending_at: datetime | None
    event_counts: dict[str, int]
    available: bool
    created_at: datetime
    updated_at: datetime


class SubscriptionPage(BaseModel):
    items: list[SubscriptionResponse]
    total: int
    next_before_id: int | None


class EventResponse(BaseModel):
    id: int
    source_event_id: str
    source_epoch: str
    state: str
    shard: str
    sequence: int
    envelope: dict[str, Any] | None
    payload_expired: bool
    first_received_at: datetime
    duplicate_count: int
    deliveries: list[dict[str, Any]]


class EventPage(BaseModel):
    items: list[EventResponse]
    total: int
    next_before_id: int | None


class QuotaAuditResponse(BaseModel):
    actual: dict[str, int]
    expected: dict[str, int]
    differences: dict[str, dict[str, int]]
    repaired: bool


class SourceConnectionResponse(BaseModel):
    source_type: Literal["kafka", "kubernetes"]
    cluster_id: str = Field(min_length=1, max_length=256)
    topic_count: int | None = Field(default=None, ge=1, le=16, strict=True)
    partition_count: int | None = Field(default=None, ge=1, le=16, strict=True)
    group_available: bool | None = None
    identity_verified: bool | None = None
