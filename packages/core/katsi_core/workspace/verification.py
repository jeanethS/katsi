"""Verifier definitions and configuration for workspace Change Set verification."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class VerifierPolicy(StrEnum):
    """Required verification policy for Change Set application."""

    OPTIONAL = "optional"
    REQUIRED_ALL = "required_all"
    REQUIRED_ANY = "required_any"
    OWNER_ONLY = "owner_only"


class VerifierApplicability(BaseModel):
    """Conditions under which a verifier applies to a Change Set."""

    risk_levels: tuple[str, ...] = ()
    operation_kinds: tuple[str, ...] = ()
    path_patterns: tuple[str, ...] = ()
    min_byte_count: int | None = None
    max_byte_count: int | None = None

    @field_validator("path_patterns")
    @classmethod
    def validate_patterns(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in v:
            if ".." in pattern or pattern.startswith("/"):
                raise ValueError("path patterns must be relative and safe")
        return v


class VerifierDefinition(BaseModel):
    """Owner-configured verifier for validating Change Set outcomes."""

    id: UUID
    display_name: str = Field(min_length=1, max_length=256)
    description: str = Field(max_length=4_000)

    # Execution configuration
    executable_path: str = Field(min_length=1)
    argument_prefix: tuple[str, ...] = ()
    variable_arg_names: tuple[str, ...] = ()

    # Scope and limits
    working_directory_scope: str | None = None  # None = workspace root
    environment_allowlist: tuple[str, ...] = ()
    timeout_seconds: float = Field(gt=0, le=3600)  # Max 1 hour
    max_output_bytes: int = Field(ge=0, le=10_000_000)  # Max 10MB

    # Verification policy
    policy: VerifierPolicy = VerifierPolicy.OPTIONAL
    applicability: VerifierApplicability = Field(default_factory=VerifierApplicability)

    @field_validator("executable_path")
    @classmethod
    def validate_executable(cls, v: str) -> str:
        if any(char in v for char in ("|", "&", ";", "$", "`", "\n", "\r")):
            raise ValueError("executable path must be safe (no shell metacharacters)")
        return v

    @field_validator("environment_allowlist")
    @classmethod
    def validate_env_vars(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for var in v:
            if not var.isidentifier() or "=" in var:
                raise ValueError(f"invalid environment variable name: {var}")
        return v


class VerifierExecution(BaseModel):
    """Result of executing a verifier against a Change Set."""

    verifier_id: UUID
    change_set_id: UUID
    exit_code: int
    signal: int | None = None
    timed_out: bool = False
    output_truncated: bool = False

    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)

    stdout_sample: str = Field(max_length=10_000)
    stderr_sample: str = Field(max_length=10_000)

    duration_seconds: float = Field(ge=0)
    occurred_at: str  # ISO format timestamp


class VerifierInvariant(BaseModel):
    """Machine-checkable invariant verified by a verifier."""

    id: UUID
    change_set_id: UUID
    verifier_id: UUID
    description: str = Field(min_length=1, max_length=4_000)
    passed: bool
    evidence: dict[str, str] = Field(default_factory=dict)


class ChangeSetVerification(BaseModel):
    """Aggregated verification status for a Change Set."""

    change_set_id: UUID
    required_verifiers_count: int = Field(ge=0)
    passed_verifiers_count: int = Field(ge=0)
    failed_verifiers_count: int = Field(ge=0)
    timeout_verifiers_count: int = Field(ge=0)
    owner_verified: bool = False

    # Verification results bounded by storage limits
    executions: tuple[VerifierExecution, ...] = ()
    invariants: tuple[VerifierInvariant, ...] = ()

    # Determined final status
    all_required_passed: bool = False
    any_required_passed: bool = False

    can_proceed_verified: bool = False
    can_proceed_unverified: bool = False


class VerificationEvidence(BaseModel):
    """Bounded verification evidence linked to a Change Set."""

    change_set_id: UUID
    evidence_type: str  # "verifier_output", "invariant_check", "owner_confirmation"
    verifier_id: UUID | None = None
    content_hash: str  # blake3 of the evidence content
    byte_size: int = Field(ge=0, le=10_000_000)
    storage_path: str | None = None
    created_at: str  # ISO format timestamp
