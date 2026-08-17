"""Verification service with pre-commit version rechecking and evidence linking."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from katsi_core.workspace.contracts import (
    ChangeSet,
    ChangeSetStatus,
    ResourceDependency,
    ResourceId,
    ResourceVersionId,
)
from katsi_core.workspace.errors import ConflictError
from katsi_core.workspace.rollback import Preimage, RollbackStep
from katsi_core.workspace.verification import (
    ChangeSetVerification,
    VerificationEvidence,
    VerifierDefinition,
    VerifierExecution,
    VerifierInvariant,
    VerifierPolicy,
)
from katsi_core.workspace.verifier_execution import VerifierExecutor, VerifierTimeoutError


class VerificationError(Exception):
    """Base exception for verification errors."""


class VersionMismatchError(VerificationError):
    """Dependency version changed during verification."""


class PrecommitCheckError(VerificationError):
    """Pre-commit version recheck failed."""


class VerificationService:
    """Coordinates verifier execution with version rechecking and evidence linking."""

    def __init__(
        self,
        executor: VerifierExecutor,
        workspace_root: Path,
        evidence_dir: Path,
    ) -> None:
        self._executor = executor
        self._workspace_root = workspace_root
        self._evidence_dir = evidence_dir
        self._evidence_dir.mkdir(parents=True, exist_ok=True)

    def verify_change_set(
        self,
        change_set: ChangeSet,
        verifiers: Sequence[VerifierDefinition],
        owner_verified: bool = False,
    ) -> ChangeSetVerification:
        """Execute applicable verifiers against a Change Set.

        Args:
            change_set: The Change Set to verify
            verifiers: Available verifier definitions
            owner_verified: Whether owner has explicitly verified

        Returns:
            ChangeSetVerification with execution results and final status
        """
        # Filter applicable verifiers
        applicable = self._filter_applicable_verifiers(change_set, verifiers)

        # Identify required verifiers
        required = [v for v in applicable if v.policy != VerifierPolicy.OPTIONAL]

        executions = []
        invariants = []
        passed_count = 0
        failed_count = 0
        timeout_count = 0

        # Execute each applicable verifier
        for verifier in applicable:
            try:
                execution = self._execute_verifier(change_set, verifier)
                executions.append(execution)

                # Check exit code
                if execution.timed_out:
                    timeout_count += 1
                elif execution.exit_code == 0:
                    passed_count += 1
                    # Extract invariants from output if available
                    invariants.extend(self._extract_invariants(change_set.id, verifier, execution))
                else:
                    failed_count += 1

            except VerifierTimeoutError:
                timeout_count += 1
            except Exception as e:
                failed_count += 1

        # Determine verification status
        required_count = len(required)
        required_passed = sum(1 for e in executions if e.exit_code == 0 and not e.timed_out)

        all_required_passed = required_passed == required_count if required_count > 0 else True
        any_required_passed = required_passed > 0 if required_count > 0 else False

        can_proceed_verified = all_required_passed and (owner_verified or required_count == 0)
        can_proceed_unverified = not can_proceed_verified and len(applicable) == 0

        return ChangeSetVerification(
            change_set_id=change_set.id,
            required_verifiers_count=required_count,
            passed_verifiers_count=passed_count,
            failed_verifiers_count=failed_count,
            timeout_verifiers_count=timeout_count,
            owner_verified=owner_verified,
            executions=tuple(executions),
            invariants=tuple(invariants),
            all_required_passed=all_required_passed,
            any_required_passed=any_required_passed,
            can_proceed_verified=can_proceed_verified,
            can_proceed_unverified=can_proceed_unverified,
        )

    def precommit_check(
        self,
        change_set: ChangeSet,
        current_versions: dict[ResourceId, ResourceVersionId],
        current_hashes: dict[ResourceId, str],
    ) -> bool:
        """Recheck input/resource versions before committing verifier results.

        Args:
            change_set: The Change Set being verified
            current_versions: Current resource version IDs from authoritative state
            current_hashes: Current content hashes from filesystem

        Returns:
            True if versions match, False if any dependency is stale

        Raises:
            PrecommitCheckError: If version recheck fails critically
        """
        for dependency in change_set.dependencies:
            resource_id = dependency.resource_id

            # Check for expected absence
            if dependency.expected_absent:
                if resource_id in current_versions or resource_id in current_hashes:
                    return False
                continue

            # Check version if specified
            if dependency.expected_version_id is not None:
                current_version = current_versions.get(resource_id)
                if current_version != dependency.expected_version_id:
                    raise VersionMismatchError(
                        f"Resource {resource_id} version mismatch: "
                        f"expected {dependency.expected_version_id}, "
                        f"current {current_version}"
                    )

            # Check content hash
            if dependency.expected_content_hash is not None:
                current_hash = current_hashes.get(resource_id)
                if current_hash != dependency.expected_content_hash:
                    raise VersionMismatchError(
                        f"Resource {resource_id} hash mismatch: "
                        f"expected {dependency.expected_content_hash}, "
                        f"current {current_hash}"
                    )

        return True

    def link_verification_evidence(
        self,
        change_set_id: UUID,
        verification: ChangeSetVerification,
    ) -> tuple[VerificationEvidence, ...]:
        """Link bounded verification evidence to the Change Set.

        Args:
            change_set_id: The Change Set to link evidence to
            verification: The verification results

        Returns:
            Tuple of VerificationEvidence records with bounded storage
        """
        evidence_records = []

        # Store execution samples as bounded evidence
        for execution in verification.executions:
            evidence = self._store_execution_evidence(change_set_id, execution)
            evidence_records.append(evidence)

        # Store invariant checks as evidence
        for invariant in verification.invariants:
            evidence = self._store_invariant_evidence(change_set_id, invariant)
            evidence_records.append(evidence)

        return tuple(evidence_records)

    def _filter_applicable_verifiers(
        self,
        change_set: ChangeSet,
        verifiers: Sequence[VerifierDefinition],
    ) -> list[VerifierDefinition]:
        """Filter verifiers that apply to this Change Set."""
        applicable = []

        for verifier in verifiers:
            # Check risk level applicability
            if verifier.applicability.risk_levels:
                if change_set.risk.value not in verifier.applicability.risk_levels:
                    continue

            # Check operation kind applicability
            if verifier.applicability.operation_kinds:
                has_matching_op = any(
                    op.kind in verifier.applicability.operation_kinds
                    for op in change_set.operations
                )
                if not has_matching_op:
                    continue

            # Check path pattern applicability
            if verifier.applicability.path_patterns:
                has_matching_path = any(
                    any(
                        Path(op.path).match(pattern)
                        for pattern in verifier.applicability.path_patterns
                    )
                    for op in change_set.operations
                )
                if not has_matching_path:
                    continue

            # Check byte count applicability
            if verifier.applicability.min_byte_count is not None:
                has_min_bytes = any(
                    op.byte_count >= verifier.applicability.min_byte_count
                    for op in change_set.operations
                )
                if not has_min_bytes:
                    continue

            if verifier.applicability.max_byte_count is not None:
                has_max_bytes = any(
                    op.byte_count <= verifier.applicability.max_byte_count
                    for op in change_set.operations
                )
                if not has_max_bytes:
                    continue

            applicable.append(verifier)

        return applicable

    def _execute_verifier(
        self,
        change_set: ChangeSet,
        verifier: VerifierDefinition,
    ) -> VerifierExecution:
        """Execute a single verifier with Change Set context."""

        # Prepare variable arguments based on Change Set
        variable_args = {
            "change_set_id": str(change_set.id),
            "workspace_id": str(change_set.workspace_id),
            "author_id": str(change_set.author_id),
            "title": change_set.title,
            "risk": change_set.risk.value,
            "operation_count": str(len(change_set.operations)),
        }

        # Prepare stdin with Change Set details
        stdin_data = json.dumps(
            {
                "id": str(change_set.id),
                "title": change_set.title,
                "risk": change_set.risk.value,
                "operations": [op.model_dump(mode="json") for op in change_set.operations],
            },
            indent=2,
        )

        return self._executor.execute(
            verifier=verifier,
            change_set_id=change_set.id,
            variable_args=variable_args,
            stdin_data=stdin_data,
        )

    def _extract_invariants(
        self,
        change_set_id: UUID,
        verifier: VerifierDefinition,
        execution: VerifierExecution,
    ) -> list[VerifierInvariant]:
        """Extract invariant checks from verifier output."""
        invariants = []

        # Try to parse structured output from verifier
        try:
            # Look for JSON in stdout
            lines = execution.stdout_sample.split("\n")
            for line in lines:
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        data = json.loads(line)
                        if "invariant" in data and "passed" in data:
                            invariant = VerifierInvariant(
                                id=uuid4(),
                                change_set_id=change_set_id,
                                verifier_id=verifier.id,
                                description=data.get("invariant", "Unnamed invariant"),
                                passed=data["passed"],
                                evidence=data.get("evidence", {}),
                            )
                            invariants.append(invariant)
                            break  # Take first valid invariant
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass  # Silent failure for unparsable output

        return invariants

    def _store_execution_evidence(
        self,
        change_set_id: UUID,
        execution: VerifierExecution,
    ) -> VerificationEvidence:
        """Store bounded verifier execution evidence."""
        import blake3

        # Prepare evidence content
        content = json.dumps(
            {
                "verifier_id": str(execution.verifier_id),
                "exit_code": execution.exit_code,
                "signal": execution.signal,
                "timed_out": execution.timed_out,
                "stdout": execution.stdout_sample,
                "stderr": execution.stderr_sample,
                "duration": execution.duration_seconds,
            },
            sort_keys=True,
        )

        # Calculate hash and size
        content_bytes = content.encode("utf-8")
        content_hash = blake3.blake3(content_bytes).hexdigest()
        byte_size = len(content_bytes)

        # Store if within limits (else evidence is hash-only)
        storage_path = None
        if byte_size <= 10_000_000:  # 10MB limit
            evidence_path = self._evidence_dir / f"{content_hash[:16]}.json"
            evidence_path.write_text(content)
            storage_path = str(evidence_path)

        return VerificationEvidence(
            change_set_id=change_set_id,
            evidence_type="verifier_output",
            verifier_id=execution.verifier_id,
            content_hash=content_hash,
            byte_size=byte_size,
            storage_path=storage_path,
            created_at=datetime.now(UTC).isoformat(),
        )

    def _store_invariant_evidence(
        self,
        change_set_id: UUID,
        invariant: VerifierInvariant,
    ) -> VerificationEvidence:
        """Store bounded invariant check evidence."""
        import blake3

        # Prepare evidence content
        content = json.dumps(
            {
                "verifier_id": str(invariant.verifier_id),
                "description": invariant.description,
                "passed": invariant.passed,
                "evidence": invariant.evidence,
            },
            sort_keys=True,
        )

        # Calculate hash and size
        content_bytes = content.encode("utf-8")
        content_hash = blake3.blake3(content_bytes).hexdigest()
        byte_size = len(content_bytes)

        # Store if within limits
        storage_path = None
        if byte_size <= 10_000_000:  # 10MB limit
            evidence_path = self._evidence_dir / f"{content_hash[:16]}.json"
            evidence_path.write_text(content)
            storage_path = str(evidence_path)

        return VerificationEvidence(
            change_set_id=change_set_id,
            evidence_type="invariant_check",
            verifier_id=invariant.verifier_id,
            content_hash=content_hash,
            byte_size=byte_size,
            storage_path=storage_path,
            created_at=datetime.now(UTC).isoformat(),
        )
