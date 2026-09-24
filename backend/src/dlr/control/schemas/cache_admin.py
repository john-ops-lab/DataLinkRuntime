"""Strict bounded DTOs for cache administration and Worker observations."""

import re
import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

CacheOperationKind = Literal["preview", "clean", "protect", "retry"]
CacheOperationStatus = Literal["pending", "running", "completed", "failed"]
CacheReason = Literal[
    "cache_in_use",
    "cache_journal_protected",
    "cache_control_reference",
    "cache_pinned",
    "cache_rebuild_unknown",
    "cache_rebuild_unavailable",
    "cache_recently_used",
    "cache_identity_unverified",
    "cache_owner_unconfirmed",
    "cache_shared_not_supported",
    "cache_scan_incomplete",
    "cache_lock_busy",
    "cache_operation_failed",
    "cache_snapshot_stale",
    "cache_worker_offline",
    "cache_worker_unsupported",
]


class CacheKeySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter_id: StrictInt = Field(gt=0)
    version_id: StrictInt = Field(gt=0)


class CacheSnapshotIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter_id: StrictInt = Field(gt=0)
    version_id: StrictInt = Field(gt=0)
    language: Literal["python", "javascript", "java", "typescript", "go"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CacheProtectSelection(CacheKeySelection):
    identity: CacheSnapshotIdentity
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    pinned: bool | None = None
    source_policy: Literal["verified_offline", "managed_online"] | None = None
    evidence_note: str | None = Field(default=None, min_length=1, max_length=256)
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def validate_action(self) -> "CacheProtectSelection":
        confirms = (
            self.source_policy is not None
            or self.evidence_note is not None
            or self.valid_until is not None
        )
        if self.pinned is None and not confirms:
            raise ValueError("protect requires pin or rebuild confirmation")
        if confirms and (
            self.source_policy is None or self.evidence_note is None or self.valid_until is None
        ):
            raise ValueError("rebuild confirmation fields must be supplied together")
        return self


class CacheOperationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: CacheOperationKind
    idempotency_key: str = Field(min_length=1, max_length=128)
    keys: list[CacheKeySelection] | None = Field(default=None, min_length=1, max_length=200)
    protect: list[CacheProtectSelection] | None = Field(default=None, min_length=1, max_length=200)
    management_operation_id: uuid.UUID | None = None
    guard_operation_id: uuid.UUID | None = None
    cleanup_id: StrictInt | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_shape(self) -> "CacheOperationCreate":
        targets = [self.management_operation_id, self.guard_operation_id, self.cleanup_id]
        if self.kind == "protect":
            if self.protect is None or self.keys is not None or any(v is not None for v in targets):
                raise ValueError("protect requires only protect items")
            pairs = [(v.adapter_id, v.version_id) for v in self.protect]
        elif self.kind == "retry":
            if (
                sum(v is not None for v in targets) != 1
                or self.keys is not None
                or self.protect is not None
            ):
                raise ValueError("retry requires exactly one target")
            pairs = []
        else:
            if self.keys is None or self.protect is not None or any(v is not None for v in targets):
                raise ValueError(f"{self.kind} requires only keys")
            pairs = [(v.adapter_id, v.version_id) for v in self.keys]
        if len(pairs) != len(set(pairs)):
            raise ValueError("cache keys must be unique")
        return self


class CacheOperationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    operation_id: uuid.UUID
    worker_id: int
    kind: CacheOperationKind
    status: CacheOperationStatus
    request: dict[str, object] = Field(validation_alias="request_payload")
    claim_epoch: int
    target_kind: str | None
    target_operation_id: uuid.UUID | None
    target_cleanup_id: int | None
    result: dict[str, object] | None
    error_code: str | None
    created_at: datetime
    claimed_at: datetime | None
    finished_at: datetime | None

    @field_validator("request", mode="before")
    @classmethod
    def validate_public_request(cls, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ValueError("invalid cache operation request")
        validated = CacheOperationCreate.model_validate(
            {**value, "idempotency_key": "audit-validation"}
        )
        return validated.model_dump(mode="json", exclude={"idempotency_key"}, exclude_none=True)


class CacheOperationPage(BaseModel):
    items: list[CacheOperationResponse]
    next_cursor: str | None


class CacheCommandClaim(BaseModel):
    operation_id: uuid.UUID
    claim_epoch: StrictInt = Field(gt=0)
    kind: CacheOperationKind
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload: dict[str, object]
    actor: str = Field(min_length=1, max_length=128)


class CacheCommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_epoch: StrictInt = Field(gt=0)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["running", "completed", "failed"]
    result: dict[str, object] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_error(self) -> "CacheCommandResult":
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("failed result requires error_code only")
        return self


class CacheSnapshotItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cache_key: str = Field(pattern=r"^[1-9][0-9]*-[1-9][0-9]*$")
    kind: Literal["version", "staging"] = "version"
    adapter_id: StrictInt = Field(gt=0)
    version_id: StrictInt = Field(gt=0)
    identity: CacheSnapshotIdentity | None = None
    digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    bytes: StrictInt = Field(ge=0)
    pinned: bool
    rebuildability: Literal["unknown", "confirmed"]
    reasons: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("reasons")
    @classmethod
    def validate_reasons(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(not v or len(v) > 64 for v in value):
            raise ValueError("invalid reasons")
        return value

    @model_validator(mode="after")
    def validate_kind(self) -> "CacheSnapshotItem":
        if self.kind == "version" and (self.identity is None or self.digest is None):
            raise ValueError("version observation requires identity and digest")
        if self.kind == "staging" and (self.identity is not None or self.digest is not None):
            raise ValueError("staging observation cannot claim ready identity")
        return self


class CacheCategorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: StrictInt = Field(ge=0)
    bytes: StrictInt = Field(ge=0)
    reclaimable_bytes: StrictInt = Field(ge=0)
    reasons: dict[str, StrictInt] = Field(default_factory=dict, max_length=32)

    @field_validator("reasons")
    @classmethod
    def validate_reason_counts(cls, value: dict[str, int]) -> dict[str, int]:
        if any(
            not re.fullmatch(r"[a-z0-9_]{1,64}", key) or count < 0 for key, count in value.items()
        ):
            raise ValueError("invalid cache reason counts")
        return value


class CacheSnapshotSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accounting: dict[str, StrictInt]
    categories: dict[str, CacheCategorySummary]
    retained_reasons: dict[str, StrictInt] = Field(default_factory=dict, max_length=32)
    policy: dict[str, int | float | bool | Literal["report_only"]]

    @field_validator("retained_reasons")
    @classmethod
    def validate_retained_reasons(cls, value: dict[str, int]) -> dict[str, int]:
        if any(
            not re.fullmatch(r"[a-z0-9_]{1,64}", key) or count < 0 for key, count in value.items()
        ):
            raise ValueError("invalid retained reasons")
        return value

    @field_validator("policy")
    @classmethod
    def validate_policy(cls, value: dict[str, object]) -> dict[str, object]:
        expected = {
            "gc_enabled",
            "pressure_gc_enabled",
            "scan_interval_seconds",
            "idle_ttl_seconds",
            "min_idle_seconds",
            "max_bytes",
            "high_watermark_percent",
            "low_watermark_percent",
            "disk_reserve_bytes",
            "max_delete_bytes_per_round",
            "max_delete_entries_per_round",
            "max_scan_entries_per_round",
            "max_scan_nodes_per_round",
            "max_scan_hash_bytes_per_round",
            "max_scan_depth",
            "max_round_seconds",
            "staging_ttl_seconds",
            "offline_protection",
            "offline_mode",
            "shared_cache_mode",
        }
        if set(value) != expected or value.get("shared_cache_mode") != "report_only":
            raise ValueError("invalid cache policy")
        return value

    @field_validator("accounting")
    @classmethod
    def validate_accounting(cls, value: dict[str, int]) -> dict[str, int]:
        if set(value) != {"committed_bytes", "reserved_bytes"} or any(
            v < 0 for v in value.values()
        ):
            raise ValueError("invalid cache accounting")
        return value

    @field_validator("categories")
    @classmethod
    def validate_categories(
        cls, value: dict[str, CacheCategorySummary]
    ) -> dict[str, CacheCategorySummary]:
        if set(value) != {"versions", "shared", "staging", "trash", "unknown"}:
            raise ValueError("invalid cache categories")
        return value


class FailedGuardItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guard_operation_id: uuid.UUID
    generation: StrictInt = Field(gt=0)
    adapter_id: StrictInt = Field(gt=0)
    version_id: StrictInt = Field(gt=0)
    operation_kind: Literal["gc", "cleanup", "replacement"]
    local_phase: Literal["failed"]
    resume_phase: str = Field(min_length=1, max_length=32)
    failure_count: StrictInt = Field(ge=3)
    error_code: str = Field(min_length=1, max_length=64)
    sampled_at: datetime


class FailedCleanupItem(BaseModel):
    cleanup_id: int
    adapter_id: int
    attempts: int
    error_code: str | None


class CacheSnapshotUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_id: uuid.UUID
    sequence: StrictInt = Field(gt=0)
    sampled_at: datetime
    state: Literal["complete", "incomplete", "owner_unconfirmed"]
    complete: bool
    cursor: str | None = Field(default=None, max_length=256)
    summary: CacheSnapshotSummary
    items: list[CacheSnapshotItem] = Field(default_factory=list, max_length=200)
    failed_guard_items: list[FailedGuardItem] = Field(default_factory=list, max_length=100)
    failed_guard_cursor: str | None = Field(default=None, max_length=64)
    failed_guard_complete: bool = True

    @model_validator(mode="after")
    def validate_sample(self) -> "CacheSnapshotUpload":
        if self.sampled_at.tzinfo is None:
            raise ValueError("sampled_at must include timezone")
        if self.sampled_at > datetime.now(UTC):
            raise ValueError("sampled_at cannot be in the future")
        if self.complete != (self.state == "complete"):
            raise ValueError("complete and state conflict")
        keys = [v.cache_key for v in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("snapshot keys must be unique")
        return self


class CacheAdminView(BaseModel):
    worker_id: int
    status: Literal[
        "unsupported", "offline", "missing", "stale", "owner_unconfirmed", "incomplete", "complete"
    ]
    sampled_at: datetime | None = None
    received_at: datetime | None = None
    sample_id: uuid.UUID | None = None
    sequence: int | None = None
    complete: bool = False
    cursor: str | None = None
    summary: CacheSnapshotSummary | None = None
    items: list[CacheSnapshotItem] = Field(default_factory=list)
    failed_guard_items: list[FailedGuardItem] = Field(default_factory=list)
    failed_guard_cursor: str | None = None
    failed_guard_complete: bool = True
    failed_cleanup_items: list[FailedCleanupItem] = Field(default_factory=list)
    failed_cleanup_next_cursor: int | None = None
