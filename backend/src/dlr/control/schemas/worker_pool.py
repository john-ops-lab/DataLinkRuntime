"""Closed routing and administrative Pool contracts."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

DbId = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
RuleName = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
]
PoolName = Annotated[
    str,
    StringConstraints(
        strict=True, strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[^\x00]+$"
    ),
]
LabelValue = Annotated[str, StringConstraints(strict=True, max_length=128, pattern=r"^[^\x00]*$")]
Selector = Annotated[dict[RuleName, LabelValue], Field(max_length=16)]
Language = Literal["python", "javascript", "typescript", "java", "go"]
Fingerprint = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
ImageDigest = Annotated[str, StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$")]
ScopeList = Annotated[list[RuleName], Field(max_length=32)]
CredentialIds = Annotated[list[DbId], Field(max_length=64)]
RouteSource = Literal[
    "adapter",
    "activation",
    "original",
    "repair",
    "current-config",
    "dependency-check",
    "legacy",
]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RouteV1(ClosedModel):
    schema_version: Literal[1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Route schema version must be an integer")
        return value


class FixedRoute(RouteV1):
    mode: Literal["fixed"]
    worker_id: DbId
    failover: Literal["none"]


class PoolRoute(RouteV1):
    mode: Literal["pool"]
    pool_id: DbId
    pool_revision: DbId
    selector: Selector
    environment_class: RuleName
    placement_scope: RuleName
    failover: Literal["compatible"]


Route = Annotated[FixedRoute | PoolRoute, Field(discriminator="mode")]


class EnvironmentRule(ClosedModel):
    name: RuleName
    image_digest: ImageDigest
    architecture: Literal["amd64", "arm64"]
    toolchains: Annotated[dict[Language, Fingerprint], Field(min_length=1)]
    builtin_materials: ScopeList = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_materials(self) -> "EnvironmentRule":
        if len(set(self.builtin_materials)) != len(self.builtin_materials):
            raise ValueError("Builtin material capabilities must be unique")
        return self


class PoolRule(ClosedModel):
    selector: Selector = Field(default_factory=dict)
    environment_class: RuleName
    placement_scope: RuleName


class PoolCreate(ClosedModel):
    name: PoolName
    rule: PoolRule


class PoolUpdate(ClosedModel):
    expected_version: DbId
    name: PoolName | None = None
    enabled: Annotated[bool, Field(strict=True)] | None = None
    rule: PoolRule | None = None


class PoolMemberUpdate(ClosedModel):
    expected_version: DbId
    worker_identity_id: UUID
    enabled: Annotated[bool, Field(strict=True)]
    labels: Selector = Field(default_factory=dict)
    placement_scopes: ScopeList = Field(default_factory=list)
    credential_ids: CredentialIds = Field(default_factory=list)
    material_scopes: ScopeList = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_grants(self) -> "PoolMemberUpdate":
        for values in (self.placement_scopes, self.credential_ids, self.material_scopes):
            if len(set(values)) != len(values):
                raise ValueError("Worker grants must be unique")
        return self


class PoolRevisionView(PoolRule):
    model_config = ConfigDict(from_attributes=True)

    revision: int
    created_at: datetime


class PoolView(BaseModel):
    id: int
    name: str
    enabled: bool
    version: int
    revision: PoolRevisionView


class PoolMemberView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    worker_id: int
    worker_identity_id: UUID
    enabled: bool
    labels: Selector
    placement_scopes: ScopeList
    credential_ids: CredentialIds
    material_scopes: ScopeList


class PoolAuditView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int
    action: str
    actor_kind: str
    actor_user_id: int | None
    details: dict[str, Any]
    created_at: datetime


class PoolDetail(PoolView):
    revisions: list[PoolRevisionView]
    members: list[PoolMemberView]
    audit: list[PoolAuditView]


class PlacementWaitPolicy(ClosedModel):
    placement_wait_timeout_seconds: Annotated[int, Field(strict=True, ge=30, le=86400)] = 900


class RouteSnapshot(ClosedModel):
    route: Route
    source: RouteSource
    required_language: Language
    environment: EnvironmentRule | Literal["unknown"]
    wait_policy: PlacementWaitPolicy | Literal["w1-fixed"]

    @model_validator(mode="after")
    def validate_environment_and_wait(self) -> "RouteSnapshot":
        if isinstance(self.route, PoolRoute):
            if (
                not isinstance(self.environment, EnvironmentRule)
                or self.environment.name != self.route.environment_class
                or not isinstance(self.wait_policy, PlacementWaitPolicy)
            ):
                raise ValueError("Pool route requires its environment and wait policy")
        elif self.wait_policy != "w1-fixed":
            raise ValueError("Fixed route requires the Wave 1 wait policy")
        return self


class RoutePreview(ClosedModel):
    route: Route | None
    source: RouteSource
