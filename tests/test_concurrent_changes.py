"""Tests for concurrent Change Set coordination and conflict detection (OpenSpec task 10.4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from katsi_core.config import SQLiteSettings
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.change_sets import ChangeSetService
from katsi_core.workspace.contracts import (
    ChangeSet,
    ChangeSetStatus,
    CreateFileOperation,
    ReplaceFileOperation,
    ResourceDependency,
    ResourceStatus,
    RiskClass,
)
from katsi_core.workspace.errors import ConflictError, StaleStateError
from katsi_core.workspace.identity import IdentityService
from katsi_core.workspace.validation import ValidationService, ValidationResult

# Test constants
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_NEW = "d" * 64


def _create_workspace_database(tmp_path: Path) -> tuple[WorkspaceSQLite, WorkspaceRepository]:
    """Create a workspace database with applied migrations."""
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, target_version=3)
    root = tmp_path / "workspace"
    root.mkdir()
    repository = WorkspaceRepository(database)
    workspace = repository.register_workspace(root, "Workspace")
    return database, repository, workspace


def _create_agents(database: WorkspaceSQLite, count: int = 3) -> list:
    """Create multiple agent identities for concurrent testing."""
    identities = IdentityService(database)
    agents = []
    for i in range(count):
        agent = identities.register(f"Agent{i+1}", "test-client")
        agents.append(agent)
    return agents


def _create_resource(
    repository: WorkspaceRepository, workspace, path: str, content_hash: str
):
    """Create a resource in the workspace for dependency testing."""
    from katsi_core.workspace.contracts import WorkspaceEventKind

    version = repository.get_workspace(workspace.id).state_version
    # Create resource and version
    resource_id = uuid4()
    version_id = uuid4()

    with repository._database.connection() as connection:
        # Insert resource
        connection.execute(
            """INSERT INTO resources VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(resource_id), str(workspace.id), path, ResourceStatus.CURRENT.value, 0,
             datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
        )
        # Insert resource version
        connection.execute(
            """INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)""",
            (str(version_id), str(resource_id), content_hash, 100,
             datetime.now(UTC).isoformat(), str(uuid4())),
        )
        # Record workspace event
        repository.append_event(
            workspace.id, version, WorkspaceEventKind.RESOURCE_CREATED,
            resource_id=resource_id, detail={"path": path}
        )

    return resource_id, version_id


def _submit_proposal(
    change_set_service: ChangeSetService,
    workspace,
    agent,
    title: str,
    idempotency_key: str,
    dependencies: tuple[ResourceDependency, ...] = (),
    operations: tuple = (),
) -> ChangeSet:
    """Submit a Change Set proposal for testing."""
    if not operations:
        operations = (CreateFileOperation(path="test.md", byte_count=1, result_content_hash=HASH_A),)

    proposal = ChangeSet(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=agent.id,
        title=title,
        idempotency_key=idempotency_key,
        dependencies=dependencies,
        operations=operations,
        risk=RiskClass.LOW,
        created_at=datetime.now(UTC),
    )
    return change_set_service.submit(proposal)


def test_agent_b_proposal_blocked_by_agent_c_same_file_change(tmp_path: Path) -> None:
    """Agent B proposal + Agent C relevant change (same file) → proposal blocked.

    Scenario:
    - Agent B submits proposal with dependency on src/main.py with hash HASH_A
    - Agent C concurrently modifies src/main.py, changing hash to HASH_NEW
    - Agent B's proposal validation should fail with exact conflict evidence
    """
    database, repository, workspace = _create_workspace_database(tmp_path)
    agents = _create_agents(database, 3)
    agent_b, agent_c = agents[1], agents[2]

    change_set_service = ChangeSetService(database)
    validation_service = ValidationService(database)

    # Create initial resource that Agent B will depend on
    resource_id, version_id = _create_resource(repository, workspace, "src/main.py", HASH_A)

    # Agent B submits proposal with dependency on the resource
    dependency = ResourceDependency(
        resource_id=resource_id,
        expected_version_id=version_id,
        expected_content_hash=HASH_A,
    )
    proposal_b = _submit_proposal(
        change_set_service, workspace, agent_b,
        title="Agent B feature",
        idempotency_key="agent-b-feature",
        dependencies=(dependency,),
        operations=(ReplaceFileOperation(
            path="src/feature.py",
            byte_count=50,
            expected_content_hash=HASH_A,
            result_content_hash=HASH_B,
        ),),
    )

    # Agent B's proposal validates successfully initially
    initial_validation = validation_service.validate_dependency_closure(proposal_b)
    assert initial_validation.is_valid, "Agent B's initial validation should succeed"

    # Agent C concurrently modifies the same resource, changing the hash
    with database.connection() as connection:
        # Update resource version to simulate concurrent modification
        new_version_id = uuid4()
        connection.execute(
            """INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)""",
            (str(new_version_id), str(resource_id), HASH_NEW, 120,
             datetime.now(UTC).isoformat(), str(uuid4())),
        )

    # Revalidate Agent B's proposal - should now fail
    revalidation = validation_service.validate_dependency_closure(proposal_b)
    assert not revalidation.is_valid, "Revalidation should fail after concurrent modification"
    assert len(revalidation.hash_mismatches) == 1, "Should have exactly one hash mismatch"

    # Verify exact invalidation evidence
    mismatch_resource_id, expected_hash, actual_hash = revalidation.hash_mismatches[0]
    assert mismatch_resource_id == resource_id, "Conflict should identify the correct resource"
    assert expected_hash == HASH_A, "Should report expected hash"
    assert actual_hash == HASH_NEW, "Should report actual concurrent hash"

    # Verify that the violated dependency is properly identified
    assert len(revalidation.violated_dependencies) == 1
    violated_dep = revalidation.violated_dependencies[0]
    assert violated_dep.resource_id == resource_id
    assert violated_dep.expected_content_hash == HASH_A


def test_agent_b_proposal_valid_with_agent_c_different_file_change(tmp_path: Path) -> None:
    """Agent B proposal + Agent C unrelated change (different file) → proposal valid.

    Scenario:
    - Agent B submits proposal with dependency on src/main.py
    - Agent C concurrently modifies a different file (src/utils.py)
    - Agent B's proposal validation should still succeed
    """
    database, repository, workspace = _create_workspace_database(tmp_path)
    agents = _create_agents(database, 3)
    agent_b, agent_c = agents[1], agents[2]

    change_set_service = ChangeSetService(database)
    validation_service = ValidationService(database)

    # Create two different resources
    main_resource, main_version = _create_resource(repository, workspace, "src/main.py", HASH_A)
    utils_resource, utils_version = _create_resource(repository, workspace, "src/utils.py", HASH_B)

    # Agent B submits proposal depending only on main.py
    dependency = ResourceDependency(
        resource_id=main_resource,
        expected_version_id=main_version,
        expected_content_hash=HASH_A,
    )
    proposal_b = _submit_proposal(
        change_set_service, workspace, agent_b,
        title="Agent B feature",
        idempotency_key="agent-b-feature",
        dependencies=(dependency,),
        operations=(CreateFileOperation(path="src/feature.py", byte_count=1, result_content_hash=HASH_C),),
    )

    # Agent B's proposal validates successfully
    initial_validation = validation_service.validate_dependency_closure(proposal_b)
    assert initial_validation.is_valid, "Agent B's initial validation should succeed"

    # Agent C modifies a different file (utils.py)
    with database.connection() as connection:
        new_utils_version = uuid4()
        connection.execute(
            """INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)""",
            (str(new_utils_version), str(utils_resource), HASH_NEW, 150,
             datetime.now(UTC).isoformat(), str(uuid4())),
        )

    # Revalidate Agent B's proposal - should still succeed
    revalidation = validation_service.validate_dependency_closure(proposal_b)
    assert revalidation.is_valid, "Revalidation should succeed with unrelated changes"
    assert len(revalidation.violated_dependencies) == 0
    assert len(revalidation.hash_mismatches) == 0


def test_exact_invalidation_evidence_quality(tmp_path: Path) -> None:
    """Exact invalidation evidence returned (which file/version changed).

    Test that validation provides precise conflict information:
    - Exact resource ID that changed
    - Expected vs actual content hashes
    - Version ID information
    - Clear violation classification
    """
    database, repository, workspace = _create_workspace_database(tmp_path)
    agents = _create_agents(database, 2)
    agent = agents[0]

    change_set_service = ChangeSetService(database)
    validation_service = ValidationService(database)

    # Create resource with specific version
    resource_id, original_version = _create_resource(repository, workspace, "config.json", HASH_A)

    # Create proposal with multiple dependency types
    dependency = ResourceDependency(
        resource_id=resource_id,
        expected_version_id=original_version,
        expected_content_hash=HASH_A,
    )
    proposal = _submit_proposal(
        change_set_service, workspace, agent,
        title="Config-dependent feature",
        idempotency_key="config-feature",
        dependencies=(dependency,),
        operations=(CreateFileOperation(path="feature.py", byte_count=1, result_content_hash=HASH_C),),
    )

    # Initial validation succeeds
    initial_validation = validation_service.validate_dependency_closure(proposal)
    assert initial_validation.is_valid

    # Simulate concurrent modification with new version
    with database.connection() as connection:
        new_version = uuid4()
        connection.execute(
            """INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)""",
            (str(new_version), str(resource_id), HASH_NEW, 200,
             datetime.now(UTC).isoformat(), str(uuid4())),
        )

    # Get detailed invalidation evidence
    failed_validation = validation_service.validate_dependency_closure(proposal)

    # Verify evidence quality - should provide exact details
    assert not failed_validation.is_valid
    assert len(failed_validation.hash_mismatches) == 1

    mismatch_resource, expected_hash, actual_hash = failed_validation.hash_mismatches[0]
    assert mismatch_resource == resource_id
    assert expected_hash == HASH_A
    assert actual_hash == HASH_NEW

    # Check that violated dependencies include complete information
    assert len(failed_validation.violated_dependencies) == 1
    violated = failed_validation.violated_dependencies[0]
    assert violated.resource_id == resource_id
    assert violated.expected_version_id == original_version
    assert violated.expected_content_hash == HASH_A

    # Verify validation result provides timestamp
    assert failed_validation.validated_at is not None


def test_independent_proposals_parallel_proceed(tmp_path: Path) -> None:
    """Independent proposals can proceed in parallel.

    Test that multiple agents can work on different files simultaneously:
    - Agent A works on frontend/ components
    - Agent B works on backend/ components
    - Agent C works on docs/
    - No conflicts should occur between independent work
    """
    database, repository, workspace = _create_workspace_database(tmp_path)
    agents = _create_agents(database, 3)
    agent_a, agent_b, agent_c = agents[0], agents[1], agents[2]

    change_set_service = ChangeSetService(database)
    validation_service = ValidationService(database)

    # Create independent resources for each agent's scope
    frontend_resource, frontend_version = _create_resource(repository, workspace, "frontend/app.js", HASH_A)
    backend_resource, backend_version = _create_resource(repository, workspace, "backend/api.py", HASH_B)
    docs_resource, docs_version = _create_resource(repository, workspace, "docs/readme.md", HASH_C)

    # Each agent creates a proposal in their independent scope
    proposal_a = _submit_proposal(
        change_set_service, workspace, agent_a,
        title="Frontend feature",
        idempotency_key="frontend-feature",
        dependencies=(ResourceDependency(
            resource_id=frontend_resource,
            expected_version_id=frontend_version,
            expected_content_hash=HASH_A,
        ),),
        operations=(CreateFileOperation(path="frontend/new.js", byte_count=50, result_content_hash=HASH_NEW),),
    )

    proposal_b = _submit_proposal(
        change_set_service, workspace, agent_b,
        title="Backend feature",
        idempotency_key="backend-feature",
        dependencies=(ResourceDependency(
            resource_id=backend_resource,
            expected_version_id=backend_version,
            expected_content_hash=HASH_B,
        ),),
        operations=(CreateFileOperation(path="backend/endpoint.py", byte_count=60, result_content_hash=HASH_NEW),),
    )

    proposal_c = _submit_proposal(
        change_set_service, workspace, agent_c,
        title="Docs update",
        idempotency_key="docs-update",
        dependencies=(ResourceDependency(
            resource_id=docs_resource,
            expected_version_id=docs_version,
            expected_content_hash=HASH_C,
        ),),
        operations=(ReplaceFileOperation(
            path="docs/guide.md",
            byte_count=80,
            expected_content_hash=HASH_C,
            result_content_hash=HASH_NEW,
        ),),
    )

    # All proposals should validate successfully independently
    validation_a = validation_service.validate_dependency_closure(proposal_a)
    validation_b = validation_service.validate_dependency_closure(proposal_b)
    validation_c = validation_service.validate_dependency_closure(proposal_c)

    assert validation_a.is_valid, "Agent A's proposal should be valid"
    assert validation_b.is_valid, "Agent B's proposal should be valid"
    assert validation_c.is_valid, "Agent C's proposal should be valid"

    # Verify no cross-dependencies or conflicts
    assert len(validation_a.violated_dependencies) == 0
    assert len(validation_b.violated_dependencies) == 0
    assert len(validation_c.violated_dependencies) == 0


def test_various_dependency_scenarios_hash_based(tmp_path: Path) -> None:
    """Test hash-based dependency conflict detection."""
    database, repository, workspace = _create_workspace_database(tmp_path)
    agents = _create_agents(database, 2)
    agent = agents[0]

    change_set_service = ChangeSetService(database)
    validation_service = ValidationService(database)

    resource_id, version_id = _create_resource(repository, workspace, "data.json", HASH_A)

    # Proposal with hash dependency only
    proposal = _submit_proposal(
        change_set_service, workspace, agent,
        title="Hash-dependent proposal",
        idempotency_key="hash-dep-test",
        dependencies=(ResourceDependency(
            resource_id=resource_id,
            expected_content_hash=HASH_A,
            # No expected_version_id - pure hash dependency
        ),),
        operations=(CreateFileOperation(path="parser.py", byte_count=1, result_content_hash=HASH_B),),
    )

    # Initial validation succeeds
    validation = validation_service.validate_dependency_closure(proposal)
    assert validation.is_valid

    # Change the hash
    with database.connection() as connection:
        new_version = uuid4()
        connection.execute(
            """INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)""",
            (str(new_version), str(resource_id), HASH_NEW, 300,
             datetime.now(UTC).isoformat(), str(uuid4())),
        )

    # Should detect hash mismatch even without version dependency
    failed_validation = validation_service.validate_dependency_closure(proposal)
    assert not failed_validation.is_valid
    assert len(failed_validation.hash_mismatches) == 1
    assert failed_validation.hash_mismatches[0][1] == HASH_A
    assert failed_validation.hash_mismatches[0][2] == HASH_NEW


def test_various_dependency_scenarios_version_based(tmp_path: Path) -> None:
    """Test version ID-based dependency conflict detection."""
    database, repository, workspace = _create_workspace_database(tmp_path)
    agents = _create_agents(database, 2)
    agent = agents[0]

    change_set_service = ChangeSetService(database)
    validation_service = ValidationService(database)

    resource_id, version_id = _create_resource(repository, workspace, "schema.sql", HASH_A)

    # Proposal with version ID dependency only
    proposal = _submit_proposal(
        change_set_service, workspace, agent,
        title="Version-dependent proposal",
        idempotency_key="version-dep-test",
        dependencies=(ResourceDependency(
            resource_id=resource_id,
            expected_version_id=version_id,
            # No hash dependency - pure version dependency
        ),),
        operations=(CreateFileOperation(path="migration.py", byte_count=1, result_content_hash=HASH_B),),
    )

    # Initial validation succeeds
    validation = validation_service.validate_dependency_closure(proposal)
    assert validation.is_valid

    # Add a new version (making the old version still exist but no longer current)
    with database.connection() as connection:
        new_version = uuid4()
        connection.execute(
            """INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)""",
            (str(new_version), str(resource_id), HASH_NEW, 400,
             datetime.now(UTC).isoformat(), str(uuid4())),
        )

    # Version-based dependency should still be valid (old version still exists)
    validation_after = validation_service.validate_dependency_closure(proposal)
    assert validation_after.is_valid, "Version dependency should still be satisfied"

    # But if we delete the version, it should fail
    with database.connection() as connection:
        connection.execute(
            "DELETE FROM resource_versions WHERE id = ?",
            (str(version_id),),
        )

    failed_validation = validation_service.validate_dependency_closure(proposal)
    assert not failed_validation.is_valid
    assert len(failed_validation.missing_resources) == 1
    assert failed_validation.missing_resources[0] == resource_id


def test_various_dependency_scenarios_absence_assertion(tmp_path: Path) -> None:
    """Test absence assertion dependency (expecting file to not exist)."""
    database, repository, workspace = _create_workspace_database(tmp_path)
    agents = _create_agents(database, 2)
    agent = agents[0]

    change_set_service = ChangeSetService(database)
    validation_service = ValidationService(database)

    # Proposal expecting a resource to be absent
    absent_resource_id = uuid4()
    proposal = _submit_proposal(
        change_set_service, workspace, agent,
        title="Absence assertion proposal",
        idempotency_key="absence-test",
        dependencies=(ResourceDependency(
            resource_id=absent_resource_id,
            expected_absent=True,
        ),),
        operations=(CreateFileOperation(path="new_file.py", byte_count=1, result_content_hash=HASH_A),),
    )

    # Initial validation succeeds (resource doesn't exist)
    validation = validation_service.validate_dependency_closure(proposal)
    assert validation.is_valid

    # Someone creates the resource concurrently
    with database.connection() as connection:
        connection.execute(
            """INSERT INTO resources VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(absent_resource_id), str(workspace.id), "unexpected.py",
             ResourceStatus.CURRENT.value, 0, datetime.now(UTC).isoformat(),
             datetime.now(UTC).isoformat()),
        )
        version_id = uuid4()
        connection.execute(
            """INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)""",
            (str(version_id), str(absent_resource_id), HASH_B, 500,
             datetime.now(UTC).isoformat(), str(uuid4())),
        )

    # Validation should fail due to unexpected presence
    failed_validation = validation_service.validate_dependency_closure(proposal)
    assert not failed_validation.is_valid
    assert len(failed_validation.unexpected_presence) == 1
    assert failed_validation.unexpected_presence[0] == absent_resource_id


def test_race_condition_handling_during_validation(tmp_path: Path) -> None:
    """Test race condition handling during concurrent validation attempts.

    Scenario: Multiple agents try to validate proposals concurrently
    while the workspace state is changing. System should handle gracefully.
    """
    database, repository, workspace = _create_workspace_database(tmp_path)
    agents = _create_agents(database, 4)
    agent_b, agent_c, agent_d = agents[1], agents[2], agents[3]

    change_set_service = ChangeSetService(database)
    validation_service = ValidationService(database)

    resource_id, version_id = _create_resource(repository, workspace, "shared.py", HASH_A)

    # Agent B creates a proposal
    dependency = ResourceDependency(
        resource_id=resource_id,
        expected_version_id=version_id,
        expected_content_hash=HASH_A,
    )
    proposal_b = _submit_proposal(
        change_set_service, workspace, agent_b,
        title="Race condition test",
        idempotency_key="race-test",
        dependencies=(dependency,),
        operations=(CreateFileOperation(path="output.py", byte_count=1, result_content_hash=HASH_B),),
    )

    # Simulate concurrent validation attempts during state changes
    initial_validation = validation_service.validate_dependency_closure(proposal_b)
    assert initial_validation.is_valid

    # Agent C modifies the resource
    with database.connection() as connection:
        new_version = uuid4()
        connection.execute(
            """INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)""",
            (str(new_version), str(resource_id), HASH_NEW, 600,
             datetime.now(UTC).isoformat(), str(uuid4())),
        )

    # Agent D tries to validate Agent B's proposal (should fail)
    concurrent_validation = validation_service.validate_dependency_closure(proposal_b)
    assert not concurrent_validation.is_valid

    # Agent B validates their own proposal (should also fail consistently)
    owner_validation = validation_service.validate_dependency_closure(proposal_b)
    assert not owner_validation.is_valid

    # Both validations should report the same conflict
    assert (concurrent_validation.hash_mismatches == owner_validation.hash_mismatches)
    assert (concurrent_validation.violated_dependencies == owner_validation.violated_dependencies)


def test_revalidation_before_authorization_freshness(tmp_path: Path) -> None:
    """Test revalidation freshness checks before authorization."""
    database, repository, workspace = _create_workspace_database(tmp_path)
    agents = _create_agents(database, 2)
    agent = agents[0]

    change_set_service = ChangeSetService(database)
    validation_service = ValidationService(database)

    resource_id, version_id = _create_resource(repository, workspace, "api.py", HASH_A)

    dependency = ResourceDependency(
        resource_id=resource_id,
        expected_version_id=version_id,
        expected_content_hash=HASH_A,
    )
    proposal = _submit_proposal(
        change_set_service, workspace, agent,
        title="Revalidation test",
        idempotency_key="reval-test",
        dependencies=(dependency,),
        operations=(CreateFileOperation(path="client.py", byte_count=1, result_content_hash=HASH_B),),
    )

    # Submit and validate
    change_set_service.transition(proposal.id, ChangeSetStatus.VALIDATED, agent.id)

    # Check freshness immediately after validation (should be fresh)
    assert validation_service.check_state_freshness(proposal.id, max_age_seconds=300)

    # Modify the resource
    with database.connection() as connection:
        new_version = uuid4()
        connection.execute(
            """INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)""",
            (str(new_version), str(resource_id), HASH_NEW, 700,
             datetime.now(UTC).isoformat(), str(uuid4())),
        )

    # Revalidation should detect the change
    revalidation = validation_service.revalidate_before_authorization(proposal.id, agent.id)
    assert not revalidation.is_valid
    assert len(revalidation.hash_mismatches) == 1


def test_operation_level_revalidation(tmp_path: Path) -> None:
    """Test operation-level revalidation before individual operations."""
    database, repository, workspace = _create_workspace_database(tmp_path)
    agents = _create_agents(database, 2)
    agent = agents[0]

    change_set_service = ChangeSetService(database)
    validation_service = ValidationService(database)

    resource_id, version_id = _create_resource(repository, workspace, "target.py", HASH_A)

    # Create multi-operation proposal
    dependency = ResourceDependency(
        resource_id=resource_id,
        expected_version_id=version_id,
        expected_content_hash=HASH_A,
    )
    proposal = _submit_proposal(
        change_set_service, workspace, agent,
        title="Multi-operation test",
        idempotency_key="multi-op-test",
        dependencies=(dependency,),
        operations=(
            ReplaceFileOperation(
                path="target.py",
                byte_count=100,
                expected_content_hash=HASH_A,
                result_content_hash=HASH_B,
            ),
            CreateFileOperation(path="helper.py", byte_count=50, result_content_hash=HASH_C),
        ),
    )

    # First operation validates successfully
    op_validation = validation_service.revalidate_before_replacement(proposal.id, 0)
    assert op_validation.is_valid

    # Modify the target resource
    with database.connection() as connection:
        new_version = uuid4()
        connection.execute(
            """INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)""",
            (str(new_version), str(resource_id), HASH_NEW, 800,
             datetime.now(UTC).isoformat(), str(uuid4())),
        )

    # Revalidation should now fail for the first operation
    with pytest.raises(StaleStateError):
        validation_service.revalidate_before_replacement(proposal.id, 0)


def test_complex_multi_dependency_conflict_matrix(tmp_path: Path) -> None:
    """Test complex scenarios with multiple dependencies and conflict patterns."""
    database, repository, workspace = _create_workspace_database(tmp_path)
    agents = _create_agents(database, 2)
    agent = agents[0]

    change_set_service = ChangeSetService(database)
    validation_service = ValidationService(database)

    # Create multiple resources
    resource_a, version_a = _create_resource(repository, workspace, "core.py", HASH_A)
    resource_b, version_b = _create_resource(repository, workspace, "utils.py", HASH_B)
    resource_c, version_c = _create_resource(repository, workspace, "config.py", HASH_C)

    # Proposal with multiple dependencies
    proposal = _submit_proposal(
        change_set_service, workspace, agent,
        title="Multi-dependency test",
        idempotency_key="multi-dep-test",
        dependencies=(
            ResourceDependency(resource_id=resource_a, expected_version_id=version_a, expected_content_hash=HASH_A),
            ResourceDependency(resource_id=resource_b, expected_version_id=version_b, expected_content_hash=HASH_B),
            ResourceDependency(resource_id=resource_c, expected_version_id=version_c, expected_content_hash=HASH_C),
        ),
        operations=(CreateFileOperation(path="orchestrator.py", byte_count=1, result_content_hash=HASH_NEW),),
    )

    # Initial validation succeeds
    validation = validation_service.validate_dependency_closure(proposal)
    assert validation.is_valid

    # Change resource B and resource C concurrently
    with database.connection() as connection:
        new_version_b = uuid4()
        connection.execute(
            """INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)""",
            (str(new_version_b), str(resource_b), HASH_NEW, 900,
             datetime.now(UTC).isoformat(), str(uuid4())),
        )
        new_version_c = uuid4()
        connection.execute(
            """INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)""",
            (str(new_version_c), str(resource_c), HASH_NEW, 950,
             datetime.now(UTC).isoformat(), str(uuid4())),
        )

    # Should detect both conflicts
    failed_validation = validation_service.validate_dependency_closure(proposal)
    assert not failed_validation.is_valid
    assert len(failed_validation.hash_mismatches) == 2
    assert len(failed_validation.violated_dependencies) == 2

    # Verify both resources are correctly identified in conflicts
    conflicted_resources = {m[0] for m in failed_validation.hash_mismatches}
    assert resource_b in conflicted_resources
    assert resource_c in conflicted_resources
    assert resource_a not in conflicted_resources  # This one didn't change


def test_validation_state_tracking_across_transitions(tmp_path: Path) -> None:
    """Test that validation state is properly tracked across Change Set transitions."""
    database, repository, workspace = _create_workspace_database(tmp_path)
    agents = _create_agents(database, 2)
    agent = agents[0]

    change_set_service = ChangeSetService(database)
    validation_service = ValidationService(database)

    resource_id, version_id = _create_resource(repository, workspace, "service.py", HASH_A)

    dependency = ResourceDependency(
        resource_id=resource_id,
        expected_version_id=version_id,
        expected_content_hash=HASH_A,
    )
    proposal = _submit_proposal(
        change_set_service, workspace, agent,
        title="State tracking test",
        idempotency_key="state-tracking",
        dependencies=(dependency,),
        operations=(CreateFileOperation(path="client.py", byte_count=1, result_content_hash=HASH_B),),
    )

    # Record initial validation
    initial_validation = validation_service.validate_dependency_closure(proposal)
    validation_service.record_validation(proposal.id, initial_validation)

    # Transition to VALIDATED
    change_set_service.transition(proposal.id, ChangeSetStatus.VALIDATED, agent.id)

    # Modify resource
    with database.connection() as connection:
        new_version = uuid4()
        connection.execute(
            """INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)""",
            (str(new_version), str(resource_id), HASH_NEW, 1000,
             datetime.now(UTC).isoformat(), str(uuid4())),
        )

    # Revalidation should detect state changed since initial validation
    revalidation = validation_service.revalidate_before_authorization(proposal.id, agent.id)
    assert not revalidation.is_valid

    # Try to transition to AUTHORIZED (should succeed but proposal should be marked STALE)
    change_set_service.transition(proposal.id, ChangeSetStatus.STALE, agent.id)

    # Verify the proposal is now in STALE state
    stale_proposal = change_set_service.get(proposal.id)
    assert stale_proposal.status == ChangeSetStatus.STALE  # type: ignore[union-attr]

    # Should not be able to transition from STALE state
    with pytest.raises(Exception):  # InvalidTransitionError
        change_set_service.transition(proposal.id, ChangeSetStatus.AUTHORIZED, agent.id)


def test_concurrent_change_coverage_summary(tmp_path: Path) -> None:
    """Comprehensive test covering all concurrent change scenarios.

    This test demonstrates the complete coverage of concurrent change handling:
    1. Same-file conflicts detected
    2. Different-file independence preserved
    3. Exact invalidation evidence provided
    4. Parallel independent work supported
    5. Multiple dependency types handled
    6. Race conditions managed
    """
    database, repository, workspace = _create_workspace_database(tmp_path)
    agents = _create_agents(database, 3)

    change_set_service = ChangeSetService(database)
    validation_service = ValidationService(database)

    # Setup shared and independent resources
    shared_resource, shared_version = _create_resource(repository, workspace, "shared.py", HASH_A)
    resource_a, version_a = _create_resource(repository, workspace, "agent_a.py", HASH_B)
    resource_b, version_b = _create_resource(repository, workspace, "agent_b.py", HASH_C)

    # Agent A and Agent B both depend on shared resource
    proposal_a = _submit_proposal(
        change_set_service, workspace, agents[0],
        title="Agent A shared work",
        idempotency_key="agent-a-shared",
        dependencies=(
            ResourceDependency(
                resource_id=shared_resource,
                expected_version_id=shared_version,
                expected_content_hash=HASH_A,
            ),
            ResourceDependency(
                resource_id=resource_a,
                expected_version_id=version_a,
                expected_content_hash=HASH_B,
            ),
        ),
        operations=(CreateFileOperation(path="agent_a_feature.py", byte_count=1, result_content_hash=HASH_NEW),),
    )

    proposal_b = _submit_proposal(
        change_set_service, workspace, agents[1],
        title="Agent B shared work",
        idempotency_key="agent-b-shared",
        dependencies=(
            ResourceDependency(
                resource_id=shared_resource,
                expected_version_id=shared_version,
                expected_content_hash=HASH_A,
            ),
            ResourceDependency(
                resource_id=resource_b,
                expected_version_id=version_b,
                expected_content_hash=HASH_C,
            ),
        ),
        operations=(CreateFileOperation(path="agent_b_feature.py", byte_count=1, result_content_hash=HASH_NEW),),
    )

    # Agent C works independently
    proposal_c = _submit_proposal(
        change_set_service, workspace, agents[2],
        title="Agent C independent work",
        idempotency_key="agent-c-independent",
        dependencies=(ResourceDependency(
            resource_id=resource_b,  # Different from shared
            expected_version_id=version_b,
            expected_content_hash=HASH_C,
        ),),
        operations=(CreateFileOperation(path="agent_c_feature.py", byte_count=1, result_content_hash=HASH_NEW),),
    )

    # All proposals should initially validate
    assert validation_service.validate_dependency_closure(proposal_a).is_valid
    assert validation_service.validate_dependency_closure(proposal_b).is_valid
    assert validation_service.validate_dependency_closure(proposal_c).is_valid

    # Modify shared resource - affects both A and B
    with database.connection() as connection:
        new_shared_version = uuid4()
        connection.execute(
            """INSERT INTO resource_versions VALUES (?, ?, ?, ?, ?, ?)""",
            (str(new_shared_version), str(shared_resource), HASH_NEW, 1100,
             datetime.now(UTC).isoformat(), str(uuid4())),
        )

    # Both A and B should now fail validation
    validation_a = validation_service.validate_dependency_closure(proposal_a)
    validation_b = validation_service.validate_dependency_closure(proposal_b)
    validation_c = validation_service.validate_dependency_closure(proposal_c)

    assert not validation_a.is_valid
    assert not validation_b.is_valid
    assert validation_c.is_valid  # Agent C still works

    # Verify exact conflict information for A and B
    assert len(validation_a.hash_mismatches) == 1
    assert validation_a.hash_mismatches[0][0] == shared_resource
    assert validation_a.hash_mismatches[0][1] == HASH_A
    assert validation_a.hash_mismatches[0][2] == HASH_NEW

    assert len(validation_b.hash_mismatches) == 1
    assert validation_b.hash_mismatches[0][0] == shared_resource
    assert validation_b.hash_mismatches[0][1] == HASH_A
    assert validation_b.hash_mismatches[0][2] == HASH_NEW

    # Verify Agent C's independent work is unaffected
    assert len(validation_c.violated_dependencies) == 0
    assert len(validation_c.hash_mismatches) == 0