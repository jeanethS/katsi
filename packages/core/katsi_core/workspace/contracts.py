"""Strict, dependency-free contracts for the authoritative workspace model."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

WorkspaceId = Annotated[UUID, Field(description="Stable workspace identifier")]
ResourceId = Annotated[UUID, Field(description="Stable logical resource identifier")]
ResourceVersionId = Annotated[UUID, Field(description="Immutable resource version identifier")]
WorkspaceEventId = Annotated[UUID, Field(description="Workspace event identifier")]
ClaimId = Annotated[UUID, Field(description="Claim identifier")]
AgentIdentityId = Annotated[UUID, Field(description="Agent identity identifier")]
CapabilityGrantId = Annotated[UUID, Field(description="Capability grant identifier")]
WorkLeaseId = Annotated[UUID, Field(description="Work lease identifier")]
ChangeSetId = Annotated[UUID, Field(description="Change Set identifier")]
ChangeSetTransitionId = Annotated[UUID, Field(description="Change Set transition identifier")]
ActionOutcomeId = Annotated[UUID, Field(description="Action outcome identifier")]


def _validate_relative_path(value: str) -> str:
    if value.startswith("/") or any(part == ".." for part in value.split("/")):
        raise ValueError("path must be workspace-relative and must not traverse parents")
    return value


RelativePath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=4096),
    AfterValidator(_validate_relative_path),
]
ContentHash = Annotated[
    str, StringConstraints(min_length=16, max_length=256, pattern=r"^[a-f0-9]+$")
]


class StrictModel(BaseModel):
    """Base for all public workspace contracts."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ImmutableModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    RELOCATED = "relocated"
    ARCHIVED = "archived"


class ResourceStatus(StrEnum):
    CURRENT = "current"
    DELETED = "deleted"
    AMBIGUOUS = "ambiguous"
    ERROR = "error"


class WorkspaceEventKind(StrEnum):
    WORKSPACE_REGISTERED = "workspace_registered"
    WORKSPACE_RELOCATED = "workspace_relocated"
    RESOURCE_CREATED = "resource_created"
    RESOURCE_UPDATED = "resource_updated"
    RESOURCE_MOVED = "resource_moved"
    RESOURCE_DELETED = "resource_deleted"
    RESOURCE_AMBIGUOUS = "resource_ambiguous"
    EXTERNAL_CHANGE = "external_change"


class ClaimStatus(StrEnum):
    PROPOSED = "proposed"
    CORROBORATED = "corroborated"
    VERIFIED = "verified"
    CONTRADICTED = "contradicted"
    SUPERSEDED = "superseded"


class CapabilityOperationClass(StrEnum):
    READ = "read"
    CLAIM = "claim"
    LEASE = "lease"
    CHANGE_SET = "change_set"
    GOVERNED_EXECUTION = "governed_execution"


class RiskClass(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class WorkLeaseKind(StrEnum):
    ADVISORY = "advisory"
    EXCLUSIVE = "exclusive"


class WorkLeaseStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


class ChangeSetStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    STALE = "stale"
    REJECTED = "rejected"
    AUTHORIZED = "authorized"
    APPLYING = "applying"
    APPLIED = "applied"
    VERIFIED = "verified"
    APPLIED_UNVERIFIED = "applied_unverified"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"


class ActionOutcomeStatus(StrEnum):
    VERIFIED = "verified"
    APPLIED_UNVERIFIED = "applied_unverified"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"
    REJECTED = "rejected"


class Workspace(StrictModel):
    id: WorkspaceId
    root_path: str = Field(min_length=1)
    display_name: str = Field(min_length=1, max_length=256)
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    state_version: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class Resource(StrictModel):
    id: ResourceId
    workspace_id: WorkspaceId
    current_path: RelativePath | None = None
    status: ResourceStatus
    state_version: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class ResourceVersion(ImmutableModel):
    id: ResourceVersionId
    resource_id: ResourceId
    content_hash: ContentHash
    byte_count: int = Field(ge=0)
    observed_at: datetime
    source_event_id: WorkspaceEventId


class WorkspaceEvent(ImmutableModel):
    id: WorkspaceEventId
    workspace_id: WorkspaceId
    sequence: int = Field(ge=1)
    kind: WorkspaceEventKind
    occurred_at: datetime
    resource_id: ResourceId | None = None
    correlation_id: ChangeSetId | None = None
    detail: dict[str, str] = Field(default_factory=dict)


class Claim(ImmutableModel):
    id: ClaimId
    workspace_id: WorkspaceId
    author_id: AgentIdentityId
    text: str = Field(min_length=1, max_length=20_000)
    scope_paths: tuple[RelativePath, ...] = ()
    confidence: float = Field(ge=0, le=1)
    status: ClaimStatus = ClaimStatus.PROPOSED
    created_at: datetime


class AgentIdentity(StrictModel):
    id: AgentIdentityId
    display_name: str = Field(min_length=1, max_length=256)
    client_name: str = Field(min_length=1, max_length=256)
    model_name: str | None = Field(default=None, max_length=256)
    process_description: str | None = Field(default=None, max_length=512)
    active: bool = True
    created_at: datetime
    revoked_at: datetime | None = None


class CapabilityGrant(StrictModel):
    id: CapabilityGrantId
    identity_id: AgentIdentityId
    workspace_id: WorkspaceId
    operation_classes: frozenset[CapabilityOperationClass] = Field(min_length=1)
    resource_scope: tuple[RelativePath, ...] = ()
    maximum_risk: RiskClass = RiskClass.LOW
    issued_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class WorkLease(StrictModel):
    id: WorkLeaseId
    workspace_id: WorkspaceId
    holder_id: AgentIdentityId
    kind: WorkLeaseKind
    status: WorkLeaseStatus = WorkLeaseStatus.ACTIVE
    task_description: str = Field(min_length=1, max_length=4_000)
    resource_scope: tuple[RelativePath, ...] = ()
    acquired_at: datetime
    expires_at: datetime
    released_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> WorkLease:
        if self.expires_at <= self.acquired_at:
            raise ValueError("expires_at must be after acquired_at")
        if self.status is WorkLeaseStatus.RELEASED and self.released_at is None:
            raise ValueError("released leases require released_at")
        return self


class ResourceDependency(ImmutableModel):
    resource_id: ResourceId
    expected_version_id: ResourceVersionId | None = None
    expected_content_hash: ContentHash | None = None
    expected_absent: bool = False

    @model_validator(mode="after")
    def _validate_assertion(self) -> ResourceDependency:
        if self.expected_absent == (
            self.expected_version_id is not None or self.expected_content_hash is not None
        ):
            raise ValueError("dependency requires either absence or an expected version/hash")
        return self


class OperationBase(ImmutableModel):
    path: RelativePath
    byte_count: int = Field(ge=0)


class CreateFileOperation(OperationBase):
    kind: Literal["create_file"] = "create_file"
    result_content_hash: ContentHash


class ReplaceFileOperation(OperationBase):
    kind: Literal["replace_file"] = "replace_file"
    expected_content_hash: ContentHash
    result_content_hash: ContentHash


class ApplyPatchOperation(OperationBase):
    kind: Literal["apply_patch"] = "apply_patch"
    expected_content_hash: ContentHash
    result_content_hash: ContentHash
    patch: str = Field(min_length=1)


class CopyFileOperation(OperationBase):
    kind: Literal["copy_file"] = "copy_file"
    source_path: RelativePath
    result_content_hash: ContentHash


class MoveFileOperation(OperationBase):
    kind: Literal["move_file"] = "move_file"
    destination_path: RelativePath
    expected_content_hash: ContentHash


class CreateDirectoryOperation(OperationBase):
    kind: Literal["create_directory"] = "create_directory"
    byte_count: int = 0


class QuarantineFileOperation(OperationBase):
    kind: Literal["quarantine_file"] = "quarantine_file"
    expected_content_hash: ContentHash


class RestoreQuarantinedFileOperation(OperationBase):
    kind: Literal["restore_quarantined_file"] = "restore_quarantined_file"
    quarantine_path: RelativePath
    result_content_hash: ContentHash


class ReplaceDerivedArtifactOperation(ReplaceFileOperation):
    kind: Literal["replace_derived_artifact"] = "replace_derived_artifact"
    source_resource_id: ResourceId


Operation = Annotated[
    CreateFileOperation
    | ReplaceFileOperation
    | ApplyPatchOperation
    | CopyFileOperation
    | MoveFileOperation
    | CreateDirectoryOperation
    | QuarantineFileOperation
    | RestoreQuarantinedFileOperation
    | ReplaceDerivedArtifactOperation,
    Field(discriminator="kind"),
]


class ChangeSet(ImmutableModel):
    id: ChangeSetId
    workspace_id: WorkspaceId
    author_id: AgentIdentityId
    title: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=1, max_length=256)
    dependencies: tuple[ResourceDependency, ...]
    operations: tuple[Operation, ...] = Field(min_length=1)
    risk: RiskClass
    status: ChangeSetStatus = ChangeSetStatus.PROPOSED
    successor_id: ChangeSetId | None = None
    created_at: datetime


_ALLOWED_TRANSITIONS: dict[ChangeSetStatus, frozenset[ChangeSetStatus]] = {
    ChangeSetStatus.PROPOSED: frozenset(
        {ChangeSetStatus.VALIDATED, ChangeSetStatus.STALE, ChangeSetStatus.REJECTED}
    ),
    ChangeSetStatus.VALIDATED: frozenset(
        {ChangeSetStatus.AUTHORIZED, ChangeSetStatus.STALE, ChangeSetStatus.REJECTED}
    ),
    ChangeSetStatus.AUTHORIZED: frozenset(
        {ChangeSetStatus.APPLYING, ChangeSetStatus.STALE, ChangeSetStatus.REJECTED}
    ),
    ChangeSetStatus.APPLYING: frozenset(
        {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLING_BACK, ChangeSetStatus.RECOVERY_REQUIRED}
    ),
    ChangeSetStatus.APPLIED: frozenset(
        {
            ChangeSetStatus.VERIFIED,
            ChangeSetStatus.APPLIED_UNVERIFIED,
            ChangeSetStatus.ROLLING_BACK,
            ChangeSetStatus.RECOVERY_REQUIRED,
        }
    ),
    ChangeSetStatus.ROLLING_BACK: frozenset(
        {ChangeSetStatus.ROLLED_BACK, ChangeSetStatus.RECOVERY_REQUIRED}
    ),
    ChangeSetStatus.STALE: frozenset(),
    ChangeSetStatus.REJECTED: frozenset(),
    ChangeSetStatus.VERIFIED: frozenset(),
    ChangeSetStatus.APPLIED_UNVERIFIED: frozenset(),
    ChangeSetStatus.ROLLED_BACK: frozenset(),
    ChangeSetStatus.RECOVERY_REQUIRED: frozenset(),
}


class ChangeSetTransition(ImmutableModel):
    id: ChangeSetTransitionId
    change_set_id: ChangeSetId
    from_status: ChangeSetStatus
    to_status: ChangeSetStatus
    actor_id: AgentIdentityId | None = None
    occurred_at: datetime
    evidence: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_transition(self) -> ChangeSetTransition:
        if self.to_status not in _ALLOWED_TRANSITIONS[self.from_status]:
            raise ValueError(
                f"invalid Change Set transition: {self.from_status} -> {self.to_status}"
            )
        return self


class ActionOutcome(ImmutableModel):
    id: ActionOutcomeId
    change_set_id: ChangeSetId
    status: ActionOutcomeStatus
    occurred_at: datetime
    receipt: dict[str, str] = Field(default_factory=dict)
