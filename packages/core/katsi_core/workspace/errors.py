"""Typed errors returned by workspace coordination services."""

from __future__ import annotations


class WorkspaceError(Exception):
    """Base error for authoritative workspace operations."""


class ConflictError(WorkspaceError):
    """The requested write conflicts with committed workspace state."""


class StaleStateError(ConflictError):
    """A command's expected workspace or resource state is no longer current."""


class AuthorizationDeniedError(WorkspaceError):
    """An identity lacks active authority for the requested operation."""


class InvalidTransitionError(WorkspaceError):
    """A lifecycle transition is not allowed by its state machine."""


class UnsupportedOperationError(WorkspaceError):
    """An operation is outside the closed governed-operation catalog."""


class ProjectionLagError(WorkspaceError):
    """A caller requires a projection that has not reached the required offset."""


class RecoveryRequiredError(WorkspaceError):
    """A prior governed action needs owner-visible recovery before proceeding."""
