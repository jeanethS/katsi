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
    INVALIDATED = "invalidated"
    CONTRADICTED = "contradicted"
    SUPERSEDED = "superseded"


class ClaimEvidenceKind(StrEnum):
    """Provenance category for evidence attached to a durable Claim."""

    AGENT = "agent"
    RESOURCE_VERSION = "resource_version"
    DETERMINISTIC = "deterministic"
    AUTHORITATIVE = "authoritative"
    OWNER = "owner"


class WorkspaceRecordKind(StrEnum):
    DECISION = "decision"
    BLOCKER = "blocker"
    OPEN_QUESTION = "open_question"


class WorkspaceRecordStatus(StrEnum):
    OPEN = "open"
    VERIFIED = "verified"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class OpenWorkStatus(StrEnum):
    OPEN = "open"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class CapabilityOperationClass(StrEnum):
    READ = "read"
    CLAIM = "claim"
    LEASE = "lease"
    CHANGE_SET = "change_set"
    GOVERNED_EXECUTION = "governed_execution"
    VIEW_SENSITIVE_LOCATION = "view_sensitive_location"
    VIEW_SENSITIVE_BIOMETRIC = "view_sensitive_biometric"
    VIEW_SENSITIVE_PERSONAL = "view_sensitive_personal"


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


class ClaimEvidence(ImmutableModel):
    id: UUID
    claim_id: ClaimId
    kind: ClaimEvidenceKind
    reference: dict[str, str] = Field(default_factory=dict)
    created_at: datetime


class ClaimTransition(ImmutableModel):
    id: UUID
    claim_id: ClaimId
    from_status: ClaimStatus
    to_status: ClaimStatus
    actor_id: AgentIdentityId | None = None
    occurred_at: datetime
    evidence: dict[str, str] = Field(default_factory=dict)


class WorkspaceRecord(ImmutableModel):
    id: UUID
    workspace_id: WorkspaceId
    author_id: AgentIdentityId
    kind: WorkspaceRecordKind
    text: str = Field(min_length=1, max_length=20_000)
    status: WorkspaceRecordStatus = WorkspaceRecordStatus.OPEN
    created_at: datetime
    updated_at: datetime


class WorkspaceRecordTransition(ImmutableModel):
    id: UUID
    record_id: UUID
    from_status: WorkspaceRecordStatus
    to_status: WorkspaceRecordStatus
    actor_id: AgentIdentityId
    occurred_at: datetime
    evidence: dict[str, str] = Field(default_factory=dict)


class OpenWork(ImmutableModel):
    id: UUID
    workspace_id: WorkspaceId
    author_id: AgentIdentityId
    description: str = Field(min_length=1, max_length=20_000)
    status: OpenWorkStatus = OpenWorkStatus.OPEN
    created_at: datetime
    updated_at: datetime


class OpenWorkTransition(ImmutableModel):
    id: UUID
    open_work_id: UUID
    from_status: OpenWorkStatus
    to_status: OpenWorkStatus
    actor_id: AgentIdentityId
    occurred_at: datetime
    evidence: dict[str, str] = Field(default_factory=dict)


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

    @property
    def identity_id(self) -> AgentIdentityId:
        """Alias for holder_id for compatibility with authorization checks."""
        return self.holder_id

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


class DerivedMediaOperationBase(OperationBase):
    """Common immutable preconditions for a derived-media workspace export.

    These operations deliberately describe *what* an owner-registered pipeline
    may produce.  They have no command, argument, URL, or source-path field,
    so an agent cannot turn a Change Set into an arbitrary processing surface.
    """

    source_resource_id: ResourceId
    source_resource_version_id: ResourceVersionId
    source_content_hash: ContentHash
    pipeline_id: str = Field(min_length=1, max_length=256)
    pipeline_fingerprint: str = Field(min_length=16, max_length=256)
    expected_output_media_type: str = Field(min_length=3, max_length=255)
    expected_output_hash: ContentHash
    max_output_bytes: int = Field(gt=0)
    source_relationship: str = Field(min_length=1, max_length=128)


class GenerateThumbnailOperation(DerivedMediaOperationBase):
    kind: Literal["generate_thumbnail"] = "generate_thumbnail"


class ExportTranscriptOrOcrOperation(DerivedMediaOperationBase):
    kind: Literal["export_transcript_or_ocr"] = "export_transcript_or_ocr"
    representation_kind: Literal["transcript_segment", "ocr_text"]


class ExportKeyframesOperation(DerivedMediaOperationBase):
    kind: Literal["export_keyframes"] = "export_keyframes"
    keyframe_ids: tuple[UUID, ...] = Field(min_length=1)


class GenerateProxyMediaOperation(DerivedMediaOperationBase):
    kind: Literal["generate_proxy_media"] = "generate_proxy_media"


class ExportRepresentationOperation(DerivedMediaOperationBase):
    kind: Literal["export_representation"] = "export_representation"
    representation_id: UUID


class ReplaceDerivedMediaArtifactOperation(DerivedMediaOperationBase):
    """Replace only an existing derived output after an exact-hash check."""

    kind: Literal["replace_derived_media_artifact"] = "replace_derived_media_artifact"
    expected_current_hash: ContentHash
    derived_artifact_source_resource_id: ResourceId


Operation = Annotated[
    CreateFileOperation
    | ReplaceFileOperation
    | ApplyPatchOperation
    | CopyFileOperation
    | MoveFileOperation
    | CreateDirectoryOperation
    | QuarantineFileOperation
    | RestoreQuarantinedFileOperation
    | ReplaceDerivedArtifactOperation
    | GenerateThumbnailOperation
    | ExportTranscriptOrOcrOperation
    | ExportKeyframesOperation
    | GenerateProxyMediaOperation
    | ExportRepresentationOperation
    | ReplaceDerivedMediaArtifactOperation,
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


class PostconditionAssertion(ImmutableModel):
    """A postcondition that must hold after a Change Set is applied."""

    resource_id: ResourceId
    expected_version_id: ResourceVersionId | None = None
    expected_content_hash: ContentHash | None = None
    expected_absent: bool = False

    @model_validator(mode="after")
    def _validate_assertion(self) -> PostconditionAssertion:
        if self.expected_absent == (
            self.expected_version_id is not None or self.expected_content_hash is not None
        ):
            raise ValueError("postcondition requires either absence or an expected version/hash")
        return self


class RollbackInformation(ImmutableModel):
    """Information needed to rollback a Change Set."""

    original_versions: dict[ResourceId, ResourceVersionId] = Field(default_factory=dict)
    quarantine_paths: tuple[RelativePath, ...] = ()
    recovery_steps: tuple[str, ...] = ()
    requires_manual_recovery: bool = False


class ChangeSetWithMetadata(ImmutableModel):
    """A Change Set with additional metadata for queries."""

    change_set: ChangeSet
    postconditions: tuple[PostconditionAssertion, ...] = ()
    rollback_info: RollbackInformation | None = None
    operation_count: int = Field(ge=0)
    total_byte_count: int = Field(ge=0)
    dependency_count: int = Field(ge=0)


class ValidationEvidence(ImmutableModel):
    """Evidence collected during Change Set validation."""

    change_set_id: ChangeSetId
    validator_id: AgentIdentityId | None = None
    validated_at: datetime
    checks_passed: tuple[str, ...] = ()
    checks_failed: tuple[str, ...] = ()
    resource_conflicts: tuple[str, ...] = ()
    dependency_satisfied: bool = True
    risk_assessment: dict[str, str] = Field(default_factory=dict)


class AuthorizationEvidence(ImmutableModel):
    """Evidence collected during Change Set authorization."""

    change_set_id: ChangeSetId
    authorizer_id: AgentIdentityId | None = None
    authorized_at: datetime
    capability_grant_id: CapabilityGrantId | None = None
    risk_approval: bool = False
    constraints: tuple[str, ...] = ()
    authorization_notes: dict[str, str] = Field(default_factory=dict)


class PortableProjectState(ImmutableModel):
    """Owner-approved project intent that may travel with a workspace."""

    schema_version: int = Field(ge=1)
    workspace_id: WorkspaceId
    display_name: str = Field(min_length=1, max_length=256)
    active_intent: str | None = Field(default=None, max_length=20_000)
    invariant_definitions: tuple[str, ...] = ()
    verified_decisions: tuple[str, ...] = ()
    selected_metadata: dict[str, str] = Field(default_factory=dict)


class BriefSection(StrEnum):
    """Logical content sections assembled into a Workspace Brief."""

    GOAL = "goal"
    CLAIM = "claim"
    DECISION = "decision"
    BLOCKER = "blocker"
    OPEN_QUESTION = "open_question"
    OPEN_WORK = "open_work"
    LEASE = "lease"
    RECENT_EVENT = "recent_event"


class BriefClaim(StrictModel):
    """A durable Claim projected into a brief with its current verification state."""

    id: ClaimId
    text: str = Field(min_length=1)
    author_id: AgentIdentityId
    status: ClaimStatus
    confidence: float = Field(ge=0, le=1)
    scope_paths: tuple[RelativePath, ...] = ()
    created_at: datetime
    invalidated: bool = False


class BriefRecord(StrictModel):
    """A workspace record (decision, blocker, or question) projected into a brief."""

    id: UUID
    kind: WorkspaceRecordKind
    text: str = Field(min_length=1)
    status: WorkspaceRecordStatus
    author_id: AgentIdentityId
    created_at: datetime


class BriefOpenWork(StrictModel):
    """Active agent work projected into a brief."""

    id: UUID
    description: str = Field(min_length=1)
    status: OpenWorkStatus
    author_id: AgentIdentityId
    created_at: datetime


class BriefLease(StrictModel):
    """An overlapping advisory Work Lease visible to other agents."""

    id: WorkLeaseId
    holder_id: AgentIdentityId
    task_description: str = Field(min_length=1)
    resource_scope: tuple[RelativePath, ...] = ()
    expires_at: datetime


class BriefRecentEvent(StrictModel):
    """A recent authoritative workspace event surfaced as fresh context."""

    event_sequence: int = Field(ge=1)
    kind: WorkspaceEventKind
    occurred_at: datetime
    path: str | None = None
    correlation_id: ChangeSetId | None = None
    detail: dict[str, str] = Field(default_factory=dict)


class ProjectionFreshness(StrictModel):
    """Lag of a rebuildable projection relative to authoritative workspace state."""

    projection_name: str = Field(min_length=1)
    applied_outbox_id: int = Field(ge=0)
    latest_outbox_id: int = Field(ge=0)
    lag: int = Field(ge=0)
    lagging: bool = False


class OmittedSection(StrictModel):
    """A brief section whose entries were held back, with the reason why."""

    section: BriefSection
    count: int = Field(ge=1)
    reason: str = Field(min_length=1)


class WorkspaceBrief(StrictModel):
    """Budget-bounded, provenance-backed snapshot of authoritative workspace state."""

    workspace_id: WorkspaceId
    state_version: int = Field(ge=0)
    last_event_sequence: int = Field(ge=0)
    intent: tuple[str, int] | None = None
    claims: tuple[BriefClaim, ...] = ()
    decisions: tuple[BriefRecord, ...] = ()
    blockers: tuple[BriefRecord, ...] = ()
    open_questions: tuple[BriefRecord, ...] = ()
    open_work: tuple[BriefOpenWork, ...] = ()
    leases: tuple[BriefLease, ...] = ()
    recent_events: tuple[BriefRecentEvent, ...] = ()
    projection_freshness: tuple[ProjectionFreshness, ...] = ()
    budget_bytes: int = Field(ge=0)
    bytes_used: int = Field(ge=0)
    omitted: tuple[OmittedSection, ...] = ()
    provisional: tuple[BriefSection, ...] = ()
    projection_lag: bool = False


class YoloModeStatus(StrEnum):
    """Lifecycle states for YOLO authorization modes."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class YoloMode(StrictModel):
    """YOLO authorization mode granting scoped auto-approval."""

    id: UUID
    workspace_id: WorkspaceId
    owner_identity_id: AgentIdentityId
    agent_identity_id: AgentIdentityId
    policy_version: str = Field(min_length=1, max_length=64)
    operation_classes: frozenset[CapabilityOperationClass] = Field(min_length=1)
    resource_scope: tuple[RelativePath, ...] = ()
    maximum_risk: RiskClass = RiskClass.LOW
    allow_derived_artifacts: bool = True
    allow_reversible_organization: bool = True
    require_owner_approval_for_originals: bool = True
    status: YoloModeStatus = YoloModeStatus.ACTIVE
    activated_at: datetime | None = None
    suspended_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class YoloAuthorization(ImmutableModel):
    """Record of auto-authorization under YOLO mode."""

    id: UUID
    yolo_mode_id: UUID
    change_set_id: ChangeSetId
    auto_authorized: bool
    policy_matched: str = Field(min_length=1, max_length=256)
    authorized_at: datetime


class YoloSuspensionEvent(ImmutableModel):
    """Record of YOLO mode suspension with reason."""

    id: UUID
    yolo_mode_id: UUID
    suspension_reason: str = Field(min_length=1, max_length=512)
    related_change_set_id: ChangeSetId | None = None
    related_event_id: UUID | None = None
    occurred_at: datetime
