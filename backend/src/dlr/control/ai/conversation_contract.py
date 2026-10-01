"""Pure, versioned contracts for lower-priority conversation context."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SourceRef(_Contract):
    sequence: int = Field(ge=1)
    revision: int = Field(ge=1)
    role: Literal["user", "assistant"]


class ConversationMessage(_Contract):
    source: SourceRef
    text: str = Field(min_length=1, max_length=16000)


class SummarySnapshot(_Contract):
    """Cited sources are provenance, not the full invalidation dependency set.

    Later persistence must compare revisions across the entire covered range,
    including covered replies that the model did not cite in this summary.
    """

    version: Literal[1]
    text: str = Field(min_length=1, max_length=8000)
    sources: tuple[SourceRef, ...] = Field(min_length=1, max_length=64)
    covered_through: int = Field(ge=1)

    @model_validator(mode="after")
    def valid_sources(self) -> Self:
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("duplicate summary sources")
        if any(source.sequence > self.covered_through for source in self.sources):
            raise ValueError("summary source exceeds coverage")
        return self


FactKind = Literal[
    "explicit_goal",
    "explicit_constraint",
    "confirmed_decision",
    "inference",
    "pending_task",
    "unresolved_question",
]


class StateFact(_Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    kind: FactKind
    confirmation_level: Literal["explicit", "confirmed", "inferred", "open"]
    text: str = Field(min_length=1, max_length=2000)
    source: SourceRef
    introduced_revision: int = Field(ge=1)
    revoked_by: SourceRef | None = None
    revoked_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def valid_authority(self) -> Self:
        expected_level = {
            "explicit_goal": "explicit",
            "explicit_constraint": "explicit",
            "confirmed_decision": "confirmed",
            "inference": "inferred",
            "pending_task": "open",
            "unresolved_question": "open",
        }[self.kind]
        if self.confirmation_level != expected_level:
            raise ValueError("fact confirmation level does not match kind")
        if (
            self.kind in ("explicit_goal", "explicit_constraint", "confirmed_decision")
            and self.source.role != "user"
        ):
            raise ValueError("explicit or confirmed facts require a user source")
        if self.kind == "inference" and self.source.role != "assistant":
            raise ValueError("inferences require an assistant source")
        if (self.revoked_by is None) != (self.revoked_revision is None):
            raise ValueError("revocation source and revision must be paired")
        if self.revoked_by is not None:
            if self.revoked_by.role != "user":
                raise ValueError("only a user source can revoke a fact")
            if self.revoked_by.sequence <= self.source.sequence:
                raise ValueError("revocation must follow the original source")
            if (
                self.revoked_revision is not None
                and self.revoked_revision <= self.introduced_revision
            ):
                raise ValueError("revocation revision must follow introduction")
        return self


class ConversationState(_Contract):
    revision: int = Field(ge=0)
    facts: tuple[StateFact, ...] = Field(default_factory=tuple, max_length=128)

    @model_validator(mode="after")
    def valid_revision(self) -> Self:
        if len({fact.id for fact in self.facts}) != len(self.facts):
            raise ValueError("duplicate state fact id")
        if any(
            fact.introduced_revision > self.revision
            or (fact.revoked_revision is not None and fact.revoked_revision > self.revision)
            for fact in self.facts
        ):
            raise ValueError("fact revision exceeds state revision")
        return self

    def active_facts(self) -> tuple[StateFact, ...]:
        """Return background facts; current user request and code outrank all of them."""
        return tuple(fact for fact in self.facts if fact.revoked_by is None)

    def with_revision(
        self,
        *,
        source: SourceRef,
        additions: tuple[StateFact, ...] = (),
        revoke_ids: tuple[str, ...] = (),
    ) -> "ConversationState":
        """Apply sourced changes without rewriting history or inferring intent."""
        if revoke_ids and source.role != "user":
            raise ValueError("only a user revision can revoke facts")
        next_revision = self.revision + 1
        latest_source = max(
            (
                max(fact.source.sequence, fact.revoked_by.sequence if fact.revoked_by else 0)
                for fact in self.facts
            ),
            default=0,
        )
        if source.sequence <= latest_source:
            raise ValueError("state source must follow existing facts")
        if len(set(revoke_ids)) != len(revoke_ids):
            raise ValueError("duplicate revocation")
        existing = {fact.id: fact for fact in self.facts}
        if any(fact_id not in existing or existing[fact_id].revoked_by for fact_id in revoke_ids):
            raise ValueError("revocation must target an active fact")
        if any(fact.id in existing for fact in additions):
            raise ValueError("new fact id already exists")
        if any(
            fact.source != source
            or fact.introduced_revision != next_revision
            or fact.revoked_by is not None
            for fact in additions
        ):
            raise ValueError("new facts must belong to this user revision")
        updated = tuple(
            fact.model_copy(update={"revoked_by": source, "revoked_revision": next_revision})
            if fact.id in revoke_ids
            else fact
            for fact in self.facts
        )
        return ConversationState(revision=next_revision, facts=updated + additions)


class FactAdditionProposal(_Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    kind: FactKind
    confirmation_level: Literal["explicit", "confirmed", "inferred", "open"]
    text: str = Field(min_length=1, max_length=2000)
    source: SourceRef
    evidence_quote: str = Field(min_length=1, max_length=500)


class FactRevocationProposal(_Contract):
    fact_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    source: SourceRef
    evidence_quote: str = Field(min_length=1, max_length=500)
