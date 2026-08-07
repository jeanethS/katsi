"""katsi core package."""

from katsi_core.workspace import (
    ActionOutcome,
    AgentIdentity,
    AuthorizationDeniedError,
    CapabilityGrant,
    ChangeSet,
    ChangeSetTransition,
    Claim,
    ConflictError,
    InvalidTransitionError,
    ProjectionLagError,
    RecoveryRequiredError,
    Resource,
    ResourceVersion,
    StaleStateError,
    UnsupportedOperationError,
    WorkLease,
    Workspace,
    WorkspaceError,
    WorkspaceEvent,
)

__version__ = "0.1.0"

__all__ = [
    "ActionOutcome",
    "AgentIdentity",
    "AuthorizationDeniedError",
    "CapabilityGrant",
    "ChangeSet",
    "ChangeSetTransition",
    "Claim",
    "ConflictError",
    "InvalidTransitionError",
    "ProjectionLagError",
    "RecoveryRequiredError",
    "Resource",
    "ResourceVersion",
    "StaleStateError",
    "UnsupportedOperationError",
    "WorkLease",
    "Workspace",
    "WorkspaceError",
    "WorkspaceEvent",
]
