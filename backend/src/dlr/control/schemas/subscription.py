"""Closed subscription management contracts; source secrets are references only."""

from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

type SourcePhase = Literal["list", "watch", "sync.started", "sync.completed", "sync.failed"]


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
        from dlr.control.services.subscription_kubernetes_identity import normalized_origin

        try:
            if any(ord(character) < 32 for character in value):
                raise ValueError
            return normalized_origin(value)
        except ValueError:
            raise ValueError(
                "Kubernetes server must be an HTTPS origin without credentials"
            ) from None

    @model_validator(mode="after")
    def supported_resource(self) -> "KubernetesSourceConfig":
        from dlr.control.services.subscription_kubernetes_identity import SUPPORTED_RESOURCES

        if (self.group, self.version, self.resource) not in SUPPORTED_RESOURCES:
            raise ValueError("Kubernetes resource is outside the verified Pod/Deployment matrix")
        return self


class QueueSourceConfig(SourceModel):
    cluster_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    server: str = Field(min_length=1, max_length=512)
    credential_id: int | None = Field(default=None, gt=0)
    ca_pem: str | None = Field(default=None, max_length=65536)
    ordering: Literal["receipt_serial"] = "receipt_serial"
    limits: dict[str, StrictInt] = Field(default_factory=dict)

    @field_validator("server")
    @classmethod
    def validate_server(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"amqp", "amqps", "grpc", "grpcs"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.port is None
                or not 1 <= parsed.port <= 65535
                or any(ord(c) < 33 for c in value)
            ):
                raise ValueError
            return value.rstrip("/")
        except ValueError:
            raise ValueError("Use a protocol://host:port address without credentials") from None


class RabbitMQSourceConfig(QueueSourceConfig):
    source_type: Literal["rabbitmq"] = "rabbitmq"
    vhost: str = Field(default="/", min_length=1, max_length=128)
    queue: str = Field(min_length=1, max_length=255)
    credential_id: int = Field(gt=0)
    mode: Literal["per_event", "business_batch"] = "per_event"

    @model_validator(mode="after")
    def rabbitmq_contract(self) -> "RabbitMQSourceConfig":
        if urlsplit(self.server).scheme not in {"amqp", "amqps"}:
            raise ValueError("RabbitMQ requires amqp:// or amqps://")
        if self.queue.startswith("amq.") or any(ord(c) < 32 for c in self.queue + self.vhost):
            raise ValueError("Use a non-reserved dedicated queue and valid virtual host")
        if self.ca_pem and urlsplit(self.server).scheme != "amqps":
            raise ValueError("A CA certificate requires TLS")
        return self


class RocketMQSourceConfig(QueueSourceConfig):
    source_type: Literal["rocketmq"] = "rocketmq"
    topic: str = Field(min_length=1, max_length=127, pattern=r"^[a-zA-Z0-9_%.-]+$")
    group_id: str = Field(min_length=1, max_length=127, pattern=r"^[a-zA-Z0-9_%.-]+$")
    mode: Literal["per_event"] = "per_event"

    @model_validator(mode="after")
    def rocketmq_contract(self) -> "RocketMQSourceConfig":
        if urlsplit(self.server).scheme not in {"grpc", "grpcs"}:
            raise ValueError("RocketMQ 5.x requires grpc:// or grpcs:// Proxy endpoint")
        if self.ca_pem and urlsplit(self.server).scheme != "grpcs":
            raise ValueError("A CA certificate requires TLS")
        if self.topic.startswith("%") or self.group_id.startswith("%"):
            raise ValueError("Use a normal topic and dedicated consumer group")
        return self


class HttpSourceConfig(SourceModel):
    source_type: Literal["http"] = "http"
    credential_id: int = Field(gt=0)
    mode: Literal["per_event", "business_batch"] = "per_event"
    ordering: Literal["receipt_serial"] = "receipt_serial"
    limits: dict[str, StrictInt] = Field(default_factory=dict)


SourceConfig = Annotated[
    KafkaSourceConfig
    | KubernetesSourceConfig
    | RabbitMQSourceConfig
    | RocketMQSourceConfig
    | HttpSourceConfig,
    Field(discriminator="source_type"),
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


class UnacceptedSkip(SubscriptionDisposition):
    shard: str = Field(min_length=1, max_length=512)
    source_event_id: str = Field(min_length=1, max_length=128)

    @field_validator("shard", "source_event_id")
    @classmethod
    def source_identifier(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Source identity contains an unsupported character")
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise ValueError("Source identity must be valid UTF-8") from None
        return value


class SubscriptionReplay(SubscriptionDisposition):
    revision_id: int | None = Field(default=None, gt=0)
    use_current_configuration: bool = False
    expected_preview_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def current_preview_required(self) -> "SubscriptionReplay":
        if self.use_current_configuration != (self.expected_preview_token is not None):
            raise ValueError("Current-configuration replay requires its exact preview token")
        return self


class CompletionAbandonment(SubscriptionDisposition):
    source_epoch: str = Field(min_length=1, max_length=64)
    receipt_id: int = Field(gt=0, strict=True)
    expected_latest_delivery_id: int = Field(gt=0, strict=True)
    expected_delivery_ordinal: int = Field(gt=0, strict=True)
    # Missing and explicitly null bind different intentions; null must be supplied.
    expected_execution_id: int | None = Field(gt=0, strict=True)
    confirm_impact: bool = Field(strict=True)

    @field_validator("source_epoch")
    @classmethod
    def valid_epoch(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("Source epoch contains an unsupported character")
        value.encode("utf-8")
        return value

    @field_validator("reason", "request_id")
    @classmethod
    def bounded_audit_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 4096:
            raise ValueError("Audit text exceeds its byte budget")
        return value

    @field_validator("confirm_impact")
    @classmethod
    def confirmed_abandonment(cls, value: bool) -> bool:
        if not value:
            raise ValueError("Completion abandonment requires explicit impact confirmation")
        return value


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


class SyncRemainingReserve(ClosedModel):
    count: int = Field(ge=0, le=3, strict=True)
    bytes: int = Field(ge=0, le=24576, strict=True)


class SyncPendingGap(ClosedModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    detected_at: datetime
    after_completion_committed: bool
    relist_required: bool


class SyncControlDiagnostic(ClosedModel):
    kind: Literal["sync.started", "sync.completed", "sync.failed"]
    receipt_id: int = Field(gt=0)
    state: (
        Literal[
            "pending", "processing", "succeeded", "failed", "skipped", "superseded", "abandoned"
        ]
        | None
    ) = None
    latest_delivery_id: int | None = Field(default=None, gt=0)
    latest_delivery_ordinal: int | None = Field(default=None, gt=0)
    execution_id: int | None = Field(default=None, gt=0)
    effective_activation_id: int | None = Field(default=None, gt=0)
    revision_override_id: int | None = Field(default=None, gt=0)


class SyncBatchSummary(ClosedModel):
    id: int = Field(gt=0)
    subscription_id: int = Field(gt=0)
    source_epoch: str | None = Field(max_length=64)
    activation_id: int | None = Field(gt=0)
    config_version: int | None = Field(gt=0)
    scope_fingerprint: str = Field(min_length=1, max_length=64)
    state: Literal["reading", "processing", "completed", "failed", "superseded", "abandoned"]
    read_complete: bool
    completion_committed: bool
    completion_abandoned: bool
    closed: bool
    drain_open: bool
    control_pending: list[Literal["sync.started", "sync.completed", "sync.failed"]] = Field(
        max_length=3
    )
    remaining_reserve: SyncRemainingReserve
    pending_gap: SyncPendingGap | None
    failure_code: str | None = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    created_at: datetime
    finished_at: datetime | None


class SyncBatchDetail(SyncBatchSummary):
    resource_version: str | None = Field(max_length=4096)
    item_count: int = Field(ge=0)
    page_count: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    controls: list[SyncControlDiagnostic] = Field(max_length=3)
    abandonment: "CompletionAbandonmentResponse | None" = None

    @field_validator("resource_version")
    @classmethod
    def bounded_resource_version(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > 4096:
            raise ValueError("Resource version exceeds its byte budget")
        return value


class SyncBatchPage(ClosedModel):
    items: list[SyncBatchSummary] = Field(max_length=100)
    total: int = Field(ge=0)
    next_before_id: int | None


class CompletionAbandonmentResponse(ClosedModel):
    batch_id: int = Field(gt=0)
    receipt_id: int = Field(gt=0)
    latest_delivery_id: int = Field(gt=0)
    execution_id: int | None
    request_id: str = Field(min_length=1, max_length=128)
    actor_id: int | None
    reason: str = Field(min_length=1, max_length=4096)
    outcome: Literal["abandoned"] = "abandoned"
    business_result: Literal["unconfirmed"] = "unconfirmed"
    cancelled_pending_delivery_id: int | None = Field(default=None, gt=0)
    disposition_id: int | None = Field(default=None, gt=0)
    settled_at: datetime


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
    limits: dict[str, int]
    global_limits: dict[str, int]
    oldest_pending_at: datetime | None
    event_counts: dict[str, int]
    available: bool
    access_level: Literal["admin", "owner", "edit", "read"] | None = None
    created_at: datetime
    updated_at: datetime
    latest_batch: SyncBatchSummary | None = None
    ingress_path: str | None = None


class SubscriptionPage(BaseModel):
    items: list[SubscriptionResponse]
    total: int
    next_before_id: int | None


class EventResponse(BaseModel):
    id: int
    source_event_id: str
    source_epoch: str
    activation_id: int
    state: str
    shard: str
    sequence: int
    envelope: dict[str, Any] | None
    payload_expired: bool
    first_received_at: datetime
    duplicate_count: int
    deliveries: list[dict[str, Any]]
    batch_id: int | None = None
    source_phase: SourcePhase | None = None
    coalesced_count: int = Field(default=0, ge=0)
    first_source_event_id: str | None = Field(default=None, max_length=128)
    superseded_by_id: int | None = Field(default=None, gt=0)
    processing_mode: Literal["per_event", "business_batch"] = "per_event"
    batch_execution_id: int | None = Field(default=None, gt=0)
    batch_member_index: int | None = Field(default=None, ge=0, lt=100)
    batch_member_outcome: Literal["succeeded", "failed", "unknown", "not_run"] | None = None
    batch_member_code: str | None = Field(default=None, max_length=64)
    batch_member_count: int | None = Field(default=None, ge=1, le=100)


class EventPage(BaseModel):
    items: list[EventResponse]
    total: int
    next_before_id: int | None


class CheckpointResponse(BaseModel):
    id: int
    shard: str
    source_epoch: str
    position: dict[str, Any]
    confirmed_position: dict[str, Any]
    assignment_generation: int
    blocked_receipt_id: int | None
    blocked_unaccepted: dict[str, Any] | None
    # DB custody positions alone cannot establish broker log-end lag.
    source_lag: int | None = None


class CheckpointPage(BaseModel):
    items: list[CheckpointResponse]
    total: int
    next_before_id: int | None


class ActivationReference(BaseModel):
    credential_id: int
    env_key: str
    field: str


class ActivationResponse(BaseModel):
    id: int
    config_version: int
    source_epoch: str
    revision_id: int
    worker_id: int
    source_credential_id: int | None
    credential_refs: list[ActivationReference]


class QuotaAuditResponse(BaseModel):
    actual: dict[str, int]
    expected: dict[str, int]
    differences: dict[str, dict[str, int]]
    repaired: bool


class KubernetesConnectionDiagnostic(ClosedModel):
    scope_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    trust_verified: bool = Field(strict=True)
    list_verified: bool = Field(strict=True)
    watch_verified: bool = Field(strict=True)
    group: str = Field(max_length=256)
    version: str = Field(min_length=1, max_length=128)
    resource: str = Field(min_length=1, max_length=128)
    namespace: str | None = Field(default=None, max_length=253)


class SourceConnectionResponse(BaseModel):
    source_type: Literal["kafka", "kubernetes", "rabbitmq", "rocketmq", "http"]
    cluster_id: str = Field(min_length=1, max_length=256)
    topic_count: int | None = Field(default=None, ge=1, le=16, strict=True)
    partition_count: int | None = Field(default=None, ge=1, le=16, strict=True)
    group_available: bool | None = None
    identity_verified: bool | None = None
    kubernetes: KubernetesConnectionDiagnostic | None = None
