"""Closed Filesystem Operation Catalog.

Strict, safety-governed filesystem operations with preflight checks, deterministic
execution, and comprehensive security validation. This is the ONLY authorized way
filesystem operations are performed in the workspace.

All operations are discriminated unions with strict validation, path canonicalization,
and rollback capability. No arbitrary commands, no permanent deletion, no privilege
escalation, no external side effects.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal

from pydantic import BaseModel, Field, field_validator

from katsi_core.workspace.errors import (
    AuthorizationDeniedError,
    InvalidTransitionError,
    UnsupportedOperationError,
    WorkspaceError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


# =============================================================================
# Operation Discriminators
# =============================================================================

class OperationKind(StrEnum):
    """Closed operation catalog - only these operations are permitted."""

    CREATE = "create"
    EXACT_HASH_REPLACE = "exact_hash_replace"
    DETERMINISTIC_PATCH = "deterministic_patch"
    COPY = "copy"
    IN_WORKSPACE_MOVE = "in_workspace_move"
    DIRECTORY_CREATE = "directory_create"
    QUARANTINE = "quarantine"
    RESTORE = "restore"
    DERIVED_ARTIFACT_REPLACE = "derived_artifact_replace"


class RiskClass(StrEnum):
    """Risk classification for operations."""

    SAFE = "safe"  # Read-only or creates new content
    LOW_RISK = "low_risk"  # Modifies content but reversible
    MEDIUM_RISK = "medium_risk"  # Modifies content with validation
    HIGH_RISK = "high_risk"  # Structural changes, quarantine


# =============================================================================
# Path Security
# =============================================================================

class PathAttackType(StrEnum):
    """Categories of path attacks we detect and reject."""

    TRAVERSAL_ATTACK = "traversal_attack"  # ../.. components
    ABSOLUTE_PATH = "absolute_path"  # starts with /
    SYMLINK_ESCAPE = "symlink_escape"  # symlinks outside workspace
    SPECIAL_FILE = "special_file"  # device files, sockets, etc.
    CROSS_WORKSPACE = "cross_workspace"  # targets different workspace
    NON_CANONICAL = "non_canonical"  # contains . or .. after resolution


class PathValidationError(WorkspaceError):
    """Path security validation failed."""

    def __init__(self, path: str, attack_type: PathAttackType, reason: str) -> None:
        self.path = path
        self.attack_type = attack_type
        self.reason = reason
        super().__init__(f"Path validation failed for {path}: {attack_type.value} - {reason}")


@dataclass(frozen=True)
class PathSecurityConfig:
    """Configuration for path security validation."""

    allow_symlinks: bool = False
    max_path_length: int = 4096
    max_filename_length: int = 255
    forbidden_names: frozenset[str] = field(
        default_factory=lambda: frozenset({
            ".", "..", "", "CON", "PRN", "AUX", "NUL",
            "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
        })
    )
    forbidden_extensions: frozenset[str] = field(
        default_factory=lambda: frozenset({
            ".exe", ".bat", ".cmd", ".com", ".scr", ".pif", ".vbs", ".js",
            ".jar", ".app", ".deb", ".rpm", ".dmg", ".pkg", ".sh",
        })
    )


def validate_path_security(
    path_str: str,
    workspace_root: Path,
    config: PathSecurityConfig | None = None,
) -> Path:
    """Validate and canonicalize a path with comprehensive security checks.

    Args:
        path_str: User-provided path (relative or absolute)
        workspace_root: The workspace root path
        config: Security configuration (uses defaults if None)

    Returns:
        Canonicalized, validated absolute path within workspace

    Raises:
        PathValidationError: If any security check fails
    """
    if config is None:
        config = PathSecurityConfig()

    # Convert to Path object
    try:
        input_path = Path(path_str)
    except Exception as e:
        raise PathValidationError(
            path_str,
            PathAttackType.NON_CANONICAL,
            f"Invalid path: {e}"
        )

    # Reject obviously problematic inputs
    if not path_str or path_str.isspace():
        raise PathValidationError(
            path_str,
            PathAttackType.NON_CANONICAL,
            "Empty or whitespace-only path"
        )

    # Check for path length limits
    if len(path_str) > config.max_path_length:
        raise PathValidationError(
            path_str,
            PathAttackType.NON_CANONICAL,
            f"Path exceeds maximum length of {config.max_path_length}"
        )

    # If path is absolute, make it relative to workspace root for validation
    if input_path.is_absolute():
        # Reject if it doesn't resolve within workspace
        try:
            resolved = input_path.resolve()
            if not str(resolved).startswith(str(workspace_root.resolve())):
                raise PathValidationError(
                    path_str,
                    PathAttackType.CROSS_WORKSPACE,
                    f"Absolute path targets different workspace: {resolved} not in {workspace_root}"
                )
            # Convert to relative for further processing
            input_path = resolved.relative_to(workspace_root.resolve())
        except ValueError as e:
            raise PathValidationError(
                path_str,
                PathAttackType.CROSS_WORKSPACE,
                f"Path resolution error: {e}"
            )

    # Check for traversal attacks in components
    components = input_path.parts
    for i, component in enumerate(components):
        # Check for traversal attempts
        if component == "..":
            raise PathValidationError(
                path_str,
                PathAttackType.TRAVERSAL_ATTACK,
                f"Path contains parent traversal at component {i}"
            )

        if component == ".":
            raise PathValidationError(
                path_str,
                PathAttackType.NON_CANONICAL,
                f"Path contains current-dir reference at component {i}"
            )

        # Check for forbidden names
        if component.upper() in config.forbidden_names:
            raise PathValidationError(
                path_str,
                PathAttackType.SPECIAL_FILE,
                f"Path contains forbidden name: {component}"
            )

        # Check filename length
        if len(component) > config.max_filename_length:
            raise PathValidationError(
                path_str,
                PathAttackType.NON_CANONICAL,
                f"Component {component} exceeds max filename length of {config.max_filename_length}"
            )

    # Build the full path
    full_path = workspace_root / input_path

    # Final canonicalization (without following symlinks)
    try:
        # Use resolve(strict=False) to not fail on non-existent files
        canonical = full_path.resolve(strict=False)

        # Verify it's still within workspace
        if not str(canonical).startswith(str(workspace_root.resolve())):
            raise PathValidationError(
                path_str,
                PathAttackType.SYMLINK_ESCAPE,
                f"Canonicalized path escapes workspace: {canonical}"
            )

        # Check if parent is a symlink (if symlinks not allowed)
        if not config.allow_symlinks:
            for parent in canonical.parents:
                if parent.exists() and parent.is_symlink():
                    raise PathValidationError(
                        path_str,
                        PathAttackType.SYMLINK_ESCAPE,
                        f"Parent path {parent} is a symbolic link"
                    )

    except Exception as e:
        if isinstance(e, PathValidationError):
            raise
        raise PathValidationError(
            path_str,
            PathAttackType.NON_CANONICAL,
            f"Path canonicalization failed: {e}"
        )

    return canonical


def validate_target_not_special_file(path: Path) -> None:
    """Reject operations on special files (devices, sockets, etc.)."""

    if not path.exists():
        return  # Non-existent files are fine

    try:
        st = path.stat()
        mode = st.st_mode

        # Reject device files
        if stat.S_ISBLK(mode) or stat.S_ISCHR(mode):
            raise PathValidationError(
                str(path),
                PathAttackType.SPECIAL_FILE,
                "Target is a device file"
            )

        # Reject sockets and named pipes
        if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode):
            raise PathValidationError(
                str(path),
                PathAttackType.SPECIAL_FILE,
                "Target is a socket or named pipe"
            )

        # Reject directories unless explicitly allowed
        if path.is_dir():
            raise PathValidationError(
                str(path),
                PathAttackType.SPECIAL_FILE,
                "Target is a directory (use directory operations)"
            )

    except OSError as e:
        raise PathValidationError(
            str(path),
            PathAttackType.SPECIAL_FILE,
            f"Failed to stat target: {e}"
        )


# =============================================================================
# Operation Limits
# =============================================================================

@dataclass
class OperationLimits:
    """Resource and risk limits for operations."""

    max_file_size: int = 100 * 1024 * 1024  # 100 MB default
    max_total_operation_bytes: int = 1024 * 1024 * 1024  # 1 GB per operation set
    max_operations_per_session: int = 10000
    max_patch_size: int = 10 * 1024 * 1024  # 10 MB patch limit
    quarantine_byte_limit: int = 1024 * 1024 * 1024  # 1 GB quarantine total
    required_free_disk_space: int = 1024 * 1024 * 1024  # 1 GB free required


# =============================================================================
# Discriminated Operation Models
# =============================================================================

class PreflightCheckResult(BaseModel):
    """Result of preflight validation."""

    passed: bool
    failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    estimated_bytes: int = 0
    estimated_operations: int = 1


class FilesystemOperation(BaseModel):
    """Base class for all filesystem operations."""

    operation_id: str = Field(description="Unique operation identifier")
    kind: OperationKind = Field(description="Operation discriminator")
    risk_class: RiskClass = Field(description="Risk classification")
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @classmethod
    def get_kind(cls) -> OperationKind:
        """Return the operation kind for this type."""
        raise NotImplementedError


class CreateOperation(FilesystemOperation):
    """Create a new file with content."""

    kind: Literal[OperationKind.CREATE] = OperationKind.CREATE
    risk_class: Literal[RiskClass.SAFE] = RiskClass.SAFE

    target_path: str = Field(description="Relative target path")
    content: bytes = Field(description="File content")
    expected_hash: str | None = Field(None, description="BLAKE3 hash of content")
    mode: int = Field(0o644, description="File permissions (no execute by default)")

    @field_validator("mode")
    @classmethod
    def validate_no_execute_bit(cls, v: int) -> int:
        """Reject execute permissions for safety."""
        if v & 0o111:
            raise ValueError("Execute permissions not allowed for safety")
        return v


class ExactHashReplaceOperation(FilesystemOperation):
    """Replace file content only if exact hash matches (idempotent)."""

    kind: Literal[OperationKind.EXACT_HASH_REPLACE] = OperationKind.EXACT_HASH_REPLACE
    risk_class: Literal[RiskClass.LOW_RISK] = RiskClass.LOW_RISK

    target_path: str = Field(description="Relative target path")
    expected_current_hash: str = Field(description="Current BLAKE3 must match")
    new_content: bytes = Field(description="New file content")
    new_hash: str = Field(description="BLAKE3 of new content")
    backup: bool = Field(True, description="Create backup before replace")


class DeterministicPatchOperation(FilesystemOperation):
    """Apply a deterministic patch to a file."""

    kind: Literal[OperationKind.DETERMINISTIC_PATCH] = OperationKind.DETERMINISTIC_PATCH
    risk_class: Literal[RiskClass.MEDIUM_RISK] = RiskClass.MEDIUM_RISK

    target_path: str = Field(description="Relative target path")
    base_hash: str = Field(description="Expected BLAKE3 before patch")
    patch_data: bytes = Field(description="Unified diff or binary patch")
    expected_output_hash: str = Field(description="BLAKE3 after patch application")
    create_backup: bool = Field(True, description="Backup before patching")


class CopyOperation(FilesystemOperation):
    """Copy a file within workspace."""

    kind: Literal[OperationKind.COPY] = OperationKind.COPY
    risk_class: Literal[RiskClass.LOW_RISK] = RiskClass.LOW_RISK

    source_path: str = Field(description="Relative source path")
    target_path: str = Field(description="Relative target path")
    expected_source_hash: str = Field(description="BLAKE3 of source")
    preserve_metadata: bool = Field(False, description="Preserve timestamps/mode")
    verify_after_copy: bool = Field(True, description="Verify hash after copy")


class InWorkspaceMoveOperation(FilesystemOperation):
    """Move/rename a file within workspace (same workspace only)."""

    kind: Literal[OperationKind.IN_WORKSPACE_MOVE] = OperationKind.IN_WORKSPACE_MOVE
    risk_class: Literal[RiskClass.MEDIUM_RISK] = RiskClass.MEDIUM_RISK

    source_path: str = Field(description="Relative source path")
    target_path: str = Field(description="Relative target path")
    expected_source_hash: str = Field(description="BLAKE3 before move")
    verify_after_move: bool = Field(True, description="Verify hash after move")
    create_undo_record: bool = Field(True, description="Record for rollback")


class DirectoryCreateOperation(FilesystemOperation):
    """Create a directory."""

    kind: Literal[OperationKind.DIRECTORY_CREATE] = OperationKind.DIRECTORY_CREATE
    risk_class: Literal[RiskClass.SAFE] = RiskClass.SAFE

    target_path: str = Field(description="Relative directory path")
    mode: int = Field(0o755, description="Directory permissions")
    create_parents: bool = Field(True, description="Create parent directories")
    fail_if_exists: bool = Field(False, description="Error if directory exists")


class QuarantineOperation(FilesystemOperation):
    """Move a file to quarantine for review."""

    kind: Literal[OperationKind.QUARANTINE] = OperationKind.QUARANTINE
    risk_class: Literal[RiskClass.HIGH_RISK] = RiskClass.HIGH_RISK

    target_path: str = Field(description="Relative path to quarantine")
    reason: str = Field(description="Why this file is quarantined")
    expected_hash: str = Field(description="BLAKE3 before quarantine")
    metadata: dict[str, str] = Field(default_factory=dict, description="Arbitrary metadata")


class RestoreOperation(FilesystemOperation):
    """Restore a file from quarantine."""

    kind: Literal[OperationKind.RESTORE] = OperationKind.RESTORE
    risk_class: Literal[RiskClass.MEDIUM_RISK] = RiskClass.MEDIUM_RISK

    quarantine_path: str = Field(description="Relative path in quarantine")
    target_path: str = Field(description="Restore destination (empty = original)")
    expected_hash: str = Field(description="BLAKE3 must match quarantine record")
    verify_after_restore: bool = Field(True)


class DerivedArtifactReplaceOperation(FilesystemOperation):
    """Replace derived artifact (build output, etc.) with validation."""

    kind: Literal[OperationKind.DERIVED_ARTIFACT_REPLACE] = OperationKind.DERIVED_ARTIFACT_REPLACE
    risk_class: Literal[RiskClass.LOW_RISK] = RiskClass.LOW_RISK

    target_path: str = Field(description="Relative target path")
    new_content: bytes = Field(description="New artifact content")
    new_hash: str = Field(description="BLAKE3 of new content")
    derivation_id: str = Field(description="Identifier of derivation process")
    expected_input_hashes: dict[str, str] = Field(
        default_factory=dict,
        description="Input file hashes that produced this artifact"
    )


# =============================================================================
# Forbidden Operations Detection
# =============================================================================

class ForbiddenOperationType(StrEnum):
    """Categories of forbidden operations."""

    ARBITRARY_COMMANDS = "arbitrary_commands"
    PERMANENT_DELETION = "permanent_deletion"
    PERMISSION_CHANGES = "permission_changes"
    OWNERSHIP_CHANGES = "ownership_changes"
    MOUNT_OPERATIONS = "mount_operations"
    DOWNLOADED_EXECUTION = "downloaded_execution"
    EXTERNAL_SIDE_EFFECTS = "external_side_effects"
    GIT_HISTORY_REWRITE = "git_history_rewrite"
    NETWORK_OPERATIONS = "network_operations"
    SYSTEM_MODIFICATIONS = "system_modifications"


class ForbiddenOperationError(WorkspaceError):
    """Attempted to execute a forbidden operation."""

    def __init__(self, operation_type: ForbiddenOperationType, reason: str) -> None:
        self.operation_type = operation_type
        self.reason = reason
        super().__init__(
            f"Forbidden operation: {operation_type.value} - {reason}"
        )


def detect_forbidden_operation(operation: FilesystemOperation) -> None:
    """Detect and reject forbidden operation patterns.

    This is a defense-in-depth check. The closed catalog already prevents
    most dangerous operations, but we verify intent hasn't been subverted.
    """
    # Check for suspicious metadata or patterns
    if hasattr(operation, "metadata"):
        metadata = operation.metadata  # type: ignore
        for key, value in metadata.items():
            # Detect command injection attempts
            if "cmd" in key.lower() or "exec" in key.lower():
                if isinstance(value, str) and any(c in value for c in [";", "&", "|", "`", "$"]):
                    raise ForbiddenOperationError(
                        ForbiddenOperationType.ARBITRARY_COMMANDS,
                        f"Metadata contains command-like patterns: {key}"
                    )

    # Check for path patterns that suggest system modification
    for field_name in ["target_path", "source_path"]:
        if hasattr(operation, field_name):
            path_value = getattr(operation, field_name)
            if isinstance(path_value, str):
                lower_path = path_value.lower()

                # System directories
                system_targets = [
                    "/etc", "/system", "/sys", "/proc", "/dev",
                    "\\windows\\system32", "\\program Files",
                    "/usr/bin", "/usr/sbin", "/bin", "/sbin",
                ]

                for system_target in system_targets:
                    if system_target.lower() in lower_path:
                        raise ForbiddenOperationError(
                            ForbiddenOperationType.SYSTEM_MODIFICATIONS,
                            f"Path targets system directory: {path_value}"
                        )

                # Git operations (we allow read-only git access, not history rewrite)
                if ".git/" in lower_path and operation.kind in [
                    OperationKind.EXACT_HASH_REPLACE,
                    OperationKind.DETERMINISTIC_PATCH,
                ]:
                    raise ForbiddenOperationError(
                        ForbiddenOperationType.GIT_HISTORY_REWRITE,
                        f"Direct .git modification not allowed: {path_value}"
                    )


# =============================================================================
# Preflight Checks
# =============================================================================

@dataclass
class PreflightContext:
    """Context for performing preflight checks."""

    workspace_root: Path
    limits: OperationLimits
    current_operation_count: int = 0
    current_byte_total: int = 0
    quarantine_size: int = 0


def perform_preflight_checks(
    operation: FilesystemOperation,
    context: PreflightContext,
) -> PreflightCheckResult:
    """Perform comprehensive preflight validation for an operation.

    Returns:
        PreflightCheckResult with pass/fail status and details
    """
    failures: list[str] = []
    warnings: list[str] = []
    estimated_bytes = 0

    # 1. Check operation count limits
    if context.current_operation_count >= context.limits.max_operations_per_session:
        failures.append(
            f"Operation count limit reached: {context.current_operation_count} >= {context.limits.max_operations_per_session}"
        )

    # 2. Path security validation
    try:
        if hasattr(operation, "target_path"):
            validate_path_security(
                operation.target_path,  # type: ignore
                context.workspace_root,
            )
            validate_target_not_special_file(
                context.workspace_root / operation.target_path  # type: ignore
            )

        if hasattr(operation, "source_path"):
            validate_path_security(
                operation.source_path,  # type: ignore
                context.workspace_root,
            )

    except PathValidationError as e:
        failures.append(f"Path validation failed: {e}")

    # 3. Operation-specific checks
    if isinstance(operation, CreateOperation):
        estimated_bytes = len(operation.content)

        # Check file size
        if estimated_bytes > context.limits.max_file_size:
            failures.append(
                f"File size {estimated_bytes} exceeds limit {context.limits.max_file_size}"
            )

        # Check total bytes
        if context.current_byte_total + estimated_bytes > context.limits.max_total_operation_bytes:
            failures.append(
                f"Total operation bytes would exceed limit: {context.current_byte_total + estimated_bytes}"
            )

    elif isinstance(operation, ExactHashReplaceOperation):
        estimated_bytes = len(operation.new_content)

        # Verify target exists and has expected hash
        target_file = context.workspace_root / operation.target_path
        if not target_file.exists():
            failures.append(f"Target file does not exist: {operation.target_path}")
        else:
            current_hash = compute_blake3_hash(target_file)
            if current_hash != operation.expected_current_hash:
                failures.append(
                    f"Hash mismatch: expected {operation.expected_current_hash}, got {current_hash}"
                )

    elif isinstance(operation, DeterministicPatchOperation):
        estimated_bytes = len(operation.patch_data)

        if estimated_bytes > context.limits.max_patch_size:
            failures.append(
                f"Patch size {estimated_bytes} exceeds limit {context.limits.max_patch_size}"
            )

        # Verify base file exists and matches expected hash
        base_file = context.workspace_root / operation.target_path
        if not base_file.exists():
            failures.append(f"Base file does not exist: {operation.target_path}")
        else:
            base_hash = compute_blake3_hash(base_file)
            if base_hash != operation.base_hash:
                failures.append(
                    f"Base hash mismatch: expected {operation.base_hash}, got {base_hash}"
                )

    elif isinstance(operation, CopyOperation):
        source_file = context.workspace_root / operation.source_path

        if not source_file.exists():
            failures.append(f"Source file does not exist: {operation.source_path}")
        else:
            estimated_bytes = source_file.stat().st_size

            # Verify source hash
            source_hash = compute_blake3_hash(source_file)
            if source_hash != operation.expected_source_hash:
                failures.append(
                    f"Source hash mismatch: expected {operation.expected_source_hash}, got {source_hash}"
                )

    elif isinstance(operation, InWorkspaceMoveOperation):
        source_file = context.workspace_root / operation.source_path

        if not source_file.exists():
            failures.append(f"Source file does not exist: {operation.source_path}")
        else:
            estimated_bytes = source_file.stat().st_size

            # Verify source hash
            source_hash = compute_blake3_hash(source_file)
            if source_hash != operation.expected_source_hash:
                failures.append(
                    f"Source hash mismatch: expected {operation.expected_source_hash}, got {source_hash}"
                )

    elif isinstance(operation, QuarantineOperation):
        target_file = context.workspace_root / operation.target_path

        if target_file.exists():
            estimated_bytes = target_file.stat().st_size

            # Verify hash
            current_hash = compute_blake3_hash(target_file)
            if current_hash != operation.expected_hash:
                failures.append(
                    f"Hash mismatch: expected {operation.expected_hash}, got {current_hash}"
                )

            # Check quarantine space
            if context.quarantine_size + estimated_bytes > context.limits.quarantine_byte_limit:
                failures.append(
                    f"Quarantine space limit reached: {context.quarantine_size + estimated_bytes}"
                )

    elif isinstance(operation, RestoreOperation):
        estimated_bytes = 0  # Size unknown until we read the quarantined file

    # 4. Disk space check
    try:
        stat = os.statvfs(context.workspace_root)
        free_space = stat.f_bavail * stat.f_frsize

        if free_space < context.limits.required_free_disk_space + estimated_bytes:
            failures.append(
                f"Insufficient disk space: {free_space} bytes free, need {context.limits.required_free_disk_space + estimated_bytes}"
            )
    except OSError as e:
        warnings.append(f"Could not check disk space: {e}")

    # 5. Forbidden operation detection
    try:
        detect_forbidden_operation(operation)
    except ForbiddenOperationError as e:
        failures.append(f"Forbidden operation detected: {e}")

    return PreflightCheckResult(
        passed=len(failures) == 0,
        failures=failures,
        warnings=warnings,
        estimated_bytes=estimated_bytes,
        estimated_operations=1,
    )


# =============================================================================
# Hash Utilities
# =============================================================================

def compute_blake3_hash(file_path: Path) -> str:
    """Compute BLAKE3 hash of a file.

    Args:
        file_path: Path to file

    Returns:
        Hex-encoded BLAKE3 hash

    Raises:
        OSError: If file cannot be read
    """
    try:
        import blake3

        hasher = blake3.blake3()
        with file_path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except ImportError:
        # Fallback to SHA256 if blake3 not available
        hasher = hashlib.sha256()
        with file_path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()


def compute_content_hash(content: bytes) -> str:
    """Compute hash of content bytes.

    Args:
        content: Bytes to hash

    Returns:
        Hex-encoded BLAKE3 (or SHA256 fallback) hash
    """
    try:
        import blake3
        return blake3.blake3(content).hexdigest()
    except ImportError:
        return hashlib.sha256(content).hexdigest()


# =============================================================================
# Deterministic In-Memory Patching
# =============================================================================

class PatchApplicationError(WorkspaceError):
    """Failed to apply patch deterministically."""


def apply_patch_in_memory(
    base_content: bytes,
    patch_data: bytes,
    expected_output_hash: str,
) -> bytes:
    """Apply a patch deterministically in memory.

    This function:
    1. Applies the patch to base content
    2. Computes the result hash
    3. Validates against expected hash
    4. Returns complete result bytes

    The patching is done entirely in memory - no files are touched.

    Args:
        base_content: Original content bytes
        patch_data: Patch bytes (unified diff or binary format)
        expected_output_hash: Expected BLAKE3 of result

    Returns:
        Patched content bytes

    Raises:
        PatchApplicationError: If patch fails or hash doesn't match
    """
    # Try to detect patch format and apply
    try:
        # Try unified diff format first
        result_content = _apply_unified_diff(base_content, patch_data)
    except Exception:
        try:
            # Fall back to binary patch
            result_content = _apply_binary_patch(base_content, patch_data)
        except Exception as e:
            raise PatchApplicationError(
                f"Failed to apply patch in memory: {e}"
            )

    # Verify output hash
    actual_hash = compute_content_hash(result_content)
    if actual_hash != expected_output_hash:
        raise PatchApplicationError(
            f"Patch output hash mismatch: expected {expected_output_hash}, got {actual_hash}"
        )

    return result_content


def _apply_unified_diff(base_content: bytes, patch_data: bytes) -> bytes:
    """Apply a unified diff patch.

    This is a simplified implementation. For production use, consider
    integrating with a proper patch library.

    Args:
        base_content: Original content
        patch_data: Unified diff bytes

    Returns:
        Patched content bytes
    """
    import io

    # Decode patch to text
    patch_text = patch_data.decode("utf-8", errors="replace")

    # Parse the unified diff
    base_lines = base_content.decode("utf-8", errors="replace").splitlines(keepends=True)
    result_lines = base_lines.copy()

    # Simple unified diff parser (handles basic hunks)
    patch_lines = patch_text.splitlines()
    i = 0
    while i < len(patch_lines):
        line = patch_lines[i]

        # Look for hunk headers
        if line.startswith("@@"):
            # Parse hunk header: @@ -old_start,old_count +new_start,new_count @@
            try:
                parts = line.split()
                hunk_info = parts[1]  # -old_start,old_count

                # Parse old line info
                if "," in hunk_info:
                    old_start, old_count = map(int, hunk_info[1:].split(","))
                else:
                    old_start = int(hunk_info[1:])
                    old_count = 1

                # Apply hunk
                i += 1
                old_line_idx = old_start - 1  # Convert to 0-based

                # Skip context lines
                while i < len(patch_lines) and patch_lines[i].startswith(" "):
                    i += 1

                # Process deletions
                while i < len(patch_lines) and patch_lines[i].startswith("-"):
                    if old_line_idx < len(result_lines):
                        result_lines.pop(old_line_idx)
                    else:
                        # Line to delete doesn't exist - error
                        raise ValueError("Invalid patch: deleting non-existent line")
                    i += 1

                # Process additions
                while i < len(patch_lines) and patch_lines[i].startswith("+"):
                    result_lines.insert(old_line_idx, patch_lines[i][1:])
                    old_line_idx += 1
                    i += 1

            except (ValueError, IndexError) as e:
                raise ValueError(f"Failed to parse hunk header: {line} - {e}")
        else:
            i += 1

    return "\n".join(result_lines).encode("utf-8")


def _apply_binary_patch(base_content: bytes, patch_data: bytes) -> bytes:
    """Apply a binary patch.

    This is a placeholder. For production, integrate with a proper
    binary patch library like bsdiff.

    Args:
        base_content: Original content
        patch_data: Binary patch

    Returns:
        Patched content bytes

    Raises:
        PatchApplicationError: If binary patch application fails
    """
    # For now, we only support unified diff
    # In production, integrate bsdiff or similar
    raise NotImplementedError("Binary patch format not yet supported")


# =============================================================================
# Operation Execution
# =============================================================================

@dataclass
class OperationResult:
    """Result of executing a filesystem operation."""

    operation_id: str
    success: bool
    error_message: str | None = None
    bytes_written: int = 0
    backup_path: str | None = None
    rollback_info: dict[str, object] | None = None
    warnings: list[str] = field(default_factory=list)


class FilesystemOperationExecutor:
    """Executor for validated filesystem operations."""

    def __init__(
        self,
        workspace_root: Path,
        limits: OperationLimits | None = None,
        quarantine_dir: Path | None = None,
    ) -> None:
        self.workspace_root = workspace_root
        self.limits = limits or OperationLimits()
        self.quarantine_dir = quarantine_dir or workspace_root / ".quarantine"
        self.operation_count = 0
        self.byte_total = 0
        self.quarantine_size = 0

        # Ensure quarantine directory exists
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)

    def execute(
        self,
        operation: FilesystemOperation,
    ) -> OperationResult:
        """Execute a validated filesystem operation.

        Args:
            operation: Validated operation to execute

        Returns:
            OperationResult with execution details
        """
        # Perform preflight checks
        context = PreflightContext(
            workspace_root=self.workspace_root,
            limits=self.limits,
            current_operation_count=self.operation_count,
            current_byte_total=self.byte_total,
            quarantine_size=self.quarantine_size,
        )

        preflight = perform_preflight_checks(operation, context)
        if not preflight.passed:
            return OperationResult(
                operation_id=operation.operation_id,
                success=False,
                error_message="Preflight checks failed: " + "; ".join(preflight.failures),
                warnings=preflight.warnings,
            )

        try:
            # Dispatch to appropriate handler
            if isinstance(operation, CreateOperation):
                result = self._execute_create(operation)
            elif isinstance(operation, ExactHashReplaceOperation):
                result = self._execute_exact_hash_replace(operation)
            elif isinstance(operation, DeterministicPatchOperation):
                result = self._execute_deterministic_patch(operation)
            elif isinstance(operation, CopyOperation):
                result = self._execute_copy(operation)
            elif isinstance(operation, InWorkspaceMoveOperation):
                result = self._execute_in_workspace_move(operation)
            elif isinstance(operation, DirectoryCreateOperation):
                result = self._execute_directory_create(operation)
            elif isinstance(operation, QuarantineOperation):
                result = self._execute_quarantine(operation)
            elif isinstance(operation, RestoreOperation):
                result = self._execute_restore(operation)
            elif isinstance(operation, DerivedArtifactReplaceOperation):
                result = self._execute_derived_artifact_replace(operation)
            else:
                return OperationResult(
                    operation_id=operation.operation_id,
                    success=False,
                    error_message=f"Unknown operation type: {type(operation)}",
                )

            # Update counters
            self.operation_count += 1
            self.byte_total += preflight.estimated_bytes

            return result

        except Exception as e:
            return OperationResult(
                operation_id=operation.operation_id,
                success=False,
                error_message=f"Execution failed: {e}",
            )

    def _execute_create(self, op: CreateOperation) -> OperationResult:
        """Execute a create operation."""
        target_path = self.workspace_root / op.target_path

        # Create parent directories
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Verify hash if provided
        if op.expected_hash:
            actual_hash = compute_content_hash(op.content)
            if actual_hash != op.expected_hash:
                return OperationResult(
                    operation_id=op.operation_id,
                    success=False,
                    error_message=f"Content hash mismatch: expected {op.expected_hash}, got {actual_hash}",
                )

        # Write content atomically
        temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
        try:
            with temp_path.open("wb") as f:
                f.write(op.content)
            os.chmod(temp_path, op.mode)
            temp_path.replace(target_path)
        except OSError as e:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Failed to write file: {e}",
            )
        finally:
            if temp_path.exists():
                temp_path.unlink()

        return OperationResult(
            operation_id=op.operation_id,
            success=True,
            bytes_written=len(op.content),
        )

    def _execute_exact_hash_replace(self, op: ExactHashReplaceOperation) -> OperationResult:
        """Execute an exact hash replace operation."""
        target_path = self.workspace_root / op.target_path

        # Verify current hash
        current_hash = compute_blake3_hash(target_path)
        if current_hash != op.expected_current_hash:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Current hash mismatch: expected {op.expected_current_hash}, got {current_hash}",
            )

        # Verify new content hash
        new_hash = compute_content_hash(op.new_content)
        if new_hash != op.new_hash:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"New content hash mismatch: expected {op.new_hash}, got {new_hash}",
            )

        # Create backup if requested
        backup_path = None
        if op.backup:
            backup_path = str(target_path.with_suffix(target_path.suffix + ".bak"))
            try:
                shutil.copy2(target_path, backup_path)
            except OSError as e:
                return OperationResult(
                    operation_id=op.operation_id,
                    success=False,
                    error_message=f"Failed to create backup: {e}",
                )

        # Write new content atomically
        temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
        try:
            with temp_path.open("wb") as f:
                f.write(op.new_content)
            temp_path.replace(target_path)
        except OSError as e:
            # Restore backup if we had one
            if backup_path and Path(backup_path).exists():
                try:
                    shutil.copy2(backup_path, target_path)
                except Exception:
                    pass
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Failed to write file: {e}",
                backup_path=backup_path,
            )
        finally:
            if temp_path.exists():
                temp_path.unlink()

        return OperationResult(
            operation_id=op.operation_id,
            success=True,
            bytes_written=len(op.new_content),
            backup_path=backup_path,
            rollback_info={"backup_path": backup_path, "original_hash": current_hash},
        )

    def _execute_deterministic_patch(self, op: DeterministicPatchOperation) -> OperationResult:
        """Execute a deterministic patch operation."""
        target_path = self.workspace_root / op.target_path

        # Read base content
        try:
            base_content = target_path.read_bytes()
        except OSError as e:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Failed to read base file: {e}",
            )

        # Verify base hash
        base_hash = compute_content_hash(base_content)
        if base_hash != op.base_hash:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Base hash mismatch: expected {op.base_hash}, got {base_hash}",
            )

        # Create backup
        backup_path = None
        if op.create_backup:
            backup_path = str(target_path.with_suffix(target_path.suffix + ".bak"))
            try:
                shutil.copy2(target_path, backup_path)
            except OSError as e:
                return OperationResult(
                    operation_id=op.operation_id,
                    success=False,
                    error_message=f"Failed to create backup: {e}",
                )

        # Apply patch in memory
        try:
            patched_content = apply_patch_in_memory(
                base_content,
                op.patch_data,
                op.expected_output_hash,
            )
        except PatchApplicationError as e:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Patch application failed: {e}",
                backup_path=backup_path,
            )

        # Write patched content atomically
        temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
        try:
            with temp_path.open("wb") as f:
                f.write(patched_content)
            temp_path.replace(target_path)
        except OSError as e:
            # Restore backup
            if backup_path and Path(backup_path).exists():
                try:
                    shutil.copy2(backup_path, target_path)
                except Exception:
                    pass
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Failed to write patched file: {e}",
                backup_path=backup_path,
            )
        finally:
            if temp_path.exists():
                temp_path.unlink()

        return OperationResult(
            operation_id=op.operation_id,
            success=True,
            bytes_written=len(patched_content),
            backup_path=backup_path,
            rollback_info={"backup_path": backup_path, "base_hash": base_hash},
        )

    def _execute_copy(self, op: CopyOperation) -> OperationResult:
        """Execute a copy operation."""
        source_path = self.workspace_root / op.source_path
        target_path = self.workspace_root / op.target_path

        # Verify source hash
        source_hash = compute_blake3_hash(source_path)
        if source_hash != op.expected_source_hash:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Source hash mismatch: expected {op.expected_source_hash}, got {source_hash}",
            )

        # Create parent directories
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Copy the file
        try:
            if op.preserve_metadata:
                shutil.copy2(source_path, target_path)
            else:
                shutil.copy(source_path, target_path)
        except OSError as e:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Copy failed: {e}",
            )

        # Verify if requested
        if op.verify_after_copy:
            target_hash = compute_blake3_hash(target_path)
            if target_hash != source_hash:
                try:
                    target_path.unlink()
                except Exception:
                    pass
                return OperationResult(
                    operation_id=op.operation_id,
                    success=False,
                    error_message=f"Copy verification failed: hash mismatch after copy",
                )

        return OperationResult(
            operation_id=op.operation_id,
            success=True,
            bytes_written=target_path.stat().st_size,
        )

    def _execute_in_workspace_move(self, op: InWorkspaceMoveOperation) -> OperationResult:
        """Execute an in-workspace move operation."""
        source_path = self.workspace_root / op.source_path
        target_path = self.workspace_root / op.target_path

        # Verify source hash
        source_hash = compute_blake3_hash(source_path)
        if source_hash != op.expected_source_hash:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Source hash mismatch: expected {op.expected_source_hash}, got {source_hash}",
            )

        # Create target parent directories
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Record for undo
        undo_info: dict[str, object] = {}
        if op.create_undo_record:
            undo_info = {
                "source_path": op.source_path,
                "target_path": op.target_path,
                "original_hash": source_hash,
            }

        # Move the file
        try:
            shutil.move(str(source_path), str(target_path))
        except OSError as e:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Move failed: {e}",
            )

        # Verify if requested
        if op.verify_after_move:
            target_hash = compute_blake3_hash(target_path)
            if target_hash != source_hash:
                # Try to move back
                try:
                    shutil.move(str(target_path), str(source_path))
                except Exception:
                    pass
                return OperationResult(
                    operation_id=op.operation_id,
                    success=False,
                    error_message=f"Move verification failed: hash mismatch after move",
                )

        return OperationResult(
            operation_id=op.operation_id,
            success=True,
            bytes_written=target_path.stat().st_size,
            rollback_info=undo_info if op.create_undo_record else None,
        )

    def _execute_directory_create(self, op: DirectoryCreateOperation) -> OperationResult:
        """Execute a directory create operation."""
        target_path = self.workspace_root / op.target_path

        # Check if exists
        if target_path.exists():
            if op.fail_if_exists:
                return OperationResult(
                    operation_id=op.operation_id,
                    success=False,
                    error_message=f"Directory already exists: {op.target_path}",
                )
            return OperationResult(
                operation_id=op.operation_id,
                success=True,
            )

        # Create directory
        try:
            if op.create_parents:
                target_path.mkdir(parents=True, exist_ok=not op.fail_if_exists)
            else:
                target_path.mkdir(exist_ok=not op.fail_if_exists)
            os.chmod(target_path, op.mode)
        except OSError as e:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Directory creation failed: {e}",
            )

        return OperationResult(
            operation_id=op.operation_id,
            success=True,
        )

    def _execute_quarantine(self, op: QuarantineOperation) -> OperationResult:
        """Execute a quarantine operation."""
        target_path = self.workspace_root / op.target_path

        # Verify hash
        current_hash = compute_blake3_hash(target_path)
        if current_hash != op.expected_hash:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Hash mismatch: expected {op.expected_hash}, got {current_hash}",
            )

        # Create quarantine subdirectory
        quarantine_path = self.quarantine_dir / op.operation_id
        quarantine_path.mkdir(parents=True, exist_ok=True)

        # Move to quarantine
        destination = quarantine_path / target_path.name
        try:
            shutil.move(str(target_path), str(destination))
        except OSError as e:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Quarantine move failed: {e}",
            )

        # Write quarantine metadata
        metadata_path = quarantine_path / "quarantine.json"
        try:
            import json
            metadata = {
                "operation_id": op.operation_id,
                "original_path": op.target_path,
                "reason": op.reason,
                "hash": op.expected_hash,
                "quarantined_at": datetime.utcnow().isoformat(),
                "metadata": op.metadata,
            }
            with metadata_path.open("w") as f:
                json.dump(metadata, f, indent=2)
        except Exception:
            pass  # Non-critical

        self.quarantine_size += destination.stat().st_size

        return OperationResult(
            operation_id=op.operation_id,
            success=True,
            rollback_info={
                "quarantine_path": str(quarantine_path.relative_to(self.workspace_root)),
                "original_path": op.target_path,
            },
        )

    def _execute_restore(self, op: RestoreOperation) -> OperationResult:
        """Execute a restore operation."""
        quarantine_path = self.quarantine_dir / op.quarantine_path

        # Verify quarantined file exists
        if not quarantine_path.exists():
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Quarantined file not found: {op.quarantine_path}",
            )

        # Find the actual file
        quarantined_files = list(quarantine_path.glob("*"))
        actual_files = [f for f in quarantined_files if f.is_file() and f.name != "quarantine.json"]

        if not actual_files:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"No restorable file found in quarantine",
            )

        quarantined_file = actual_files[0]

        # Verify hash
        quarantined_hash = compute_blake3_hash(quarantined_file)
        if quarantined_hash != op.expected_hash:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Hash mismatch: expected {op.expected_hash}, got {quarantined_hash}",
            )

        # Determine target path
        if op.target_path:
            target_path = self.workspace_root / op.target_path
        else:
            # Try to read from quarantine metadata
            metadata_path = quarantine_path / "quarantine.json"
            if metadata_path.exists():
                try:
                    import json
                    with metadata_path.open("r") as f:
                        metadata = json.load(f)
                    target_path = self.workspace_root / metadata["original_path"]
                except Exception:
                    target_path = self.workspace_root / quarantined_file.name
            else:
                target_path = self.workspace_root / quarantined_file.name

        # Create parent directories
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Restore file
        try:
            shutil.copy2(quarantined_file, target_path)
        except OSError as e:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Restore failed: {e}",
            )

        # Verify if requested
        if op.verify_after_restore:
            restored_hash = compute_blake3_hash(target_path)
            if restored_hash != op.expected_hash:
                return OperationResult(
                    operation_id=op.operation_id,
                    success=False,
                    error_message=f"Restore verification failed: hash mismatch",
                )

        return OperationResult(
            operation_id=op.operation_id,
            success=True,
            bytes_written=target_path.stat().st_size,
        )

    def _execute_derived_artifact_replace(self, op: DerivedArtifactReplaceOperation) -> OperationResult:
        """Execute a derived artifact replace operation."""
        target_path = self.workspace_root / op.target_path

        # Verify new content hash
        new_hash = compute_content_hash(op.new_content)
        if new_hash != op.new_hash:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Content hash mismatch: expected {op.new_hash}, got {new_hash}",
            )

        # Create parent directories
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Write atomically
        temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
        try:
            with temp_path.open("wb") as f:
                f.write(op.new_content)
            temp_path.replace(target_path)
        except OSError as e:
            return OperationResult(
                operation_id=op.operation_id,
                success=False,
                error_message=f"Failed to write artifact: {e}",
            )
        finally:
            if temp_path.exists():
                temp_path.unlink()

        return OperationResult(
            operation_id=op.operation_id,
            success=True,
            bytes_written=len(op.new_content),
            rollback_info={
                "derivation_id": op.derivation_id,
                "expected_input_hashes": op.expected_input_hashes,
            },
        )


# =============================================================================
# Utilities
# =============================================================================

def create_operation_id() -> str:
    """Create a unique operation ID."""
    import uuid
    return f"op_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def validate_operation_limits(limits: OperationLimits) -> None:
    """Validate operation limits are reasonable.

    Raises:
        ValueError: If limits are invalid
    """
    if limits.max_file_size <= 0:
        raise ValueError("max_file_size must be positive")

    if limits.max_total_operation_bytes <= 0:
        raise ValueError("max_total_operation_bytes must be positive")

    if limits.max_operations_per_session <= 0:
        raise ValueError("max_operations_per_session must be positive")

    if limits.max_patch_size > limits.max_file_size:
        raise ValueError("max_patch_size cannot exceed max_file_size")

    if limits.quarantine_byte_limit <= 0:
        raise ValueError("quarantine_byte_limit must be positive")

    if limits.required_free_disk_space <= 0:
        raise ValueError("required_free_disk_space must be positive")
