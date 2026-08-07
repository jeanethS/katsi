"""Tests for public workspace coordination contracts."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from katsi_core import (
    ActionOutcome,
    AgentIdentity,
    CapabilityGrant,
    ChangeSet,
    ChangeSetTransition,
    Claim,
    Resource,
    ResourceVersion,
    WorkLease,
    Workspace,
    WorkspaceEvent,
)
from katsi_core.config import Settings, VerifierDefinitionSettings
from katsi_core.workspace.contracts import (
    ActionOutcomeStatus,
    CapabilityOperationClass,
    ChangeSetStatus,
    CreateFileOperation,
    Operation,
    ResourceDependency,
    ResourceStatus,
    RiskClass,
    WorkLeaseKind,
    WorkspaceEventKind,
)

NOW = datetime(2026, 8, 7, tzinfo=UTC)
HASH = "a" * 64


def test_workspace_contract_round_trips_as_json() -> None:
    workspace = Workspace(
        id=uuid4(),
        root_path="/projects/katsi",
        display_name="Katsi",
        state_version=0,
        created_at=NOW,
        updated_at=NOW,
    )

    restored = Workspace.model_validate_json(workspace.model_dump_json())

    assert restored == workspace


def test_every_public_model_contract_round_trips_as_json() -> None:
    workspace_id = uuid4()
    resource_id = uuid4()
    version_id = uuid4()
    event_id = uuid4()
    identity_id = uuid4()
    change_set_id = uuid4()
    resource_version = ResourceVersion(
        id=version_id,
        resource_id=resource_id,
        content_hash=HASH,
        byte_count=12,
        observed_at=NOW,
        source_event_id=event_id,
    )
    operation = CreateFileOperation(path="brief.md", byte_count=1, result_content_hash=HASH)
    change_set = ChangeSet(
        id=change_set_id,
        workspace_id=workspace_id,
        author_id=identity_id,
        title="Add brief",
        idempotency_key="brief-v1",
        dependencies=(ResourceDependency(resource_id=resource_id, expected_version_id=version_id),),
        operations=(operation,),
        risk=RiskClass.LOW,
        created_at=NOW,
    )
    models: list[BaseModel] = [
        Workspace(
            id=workspace_id,
            root_path="/projects/katsi",
            display_name="Katsi",
            state_version=0,
            created_at=NOW,
            updated_at=NOW,
        ),
        Resource(
            id=resource_id,
            workspace_id=workspace_id,
            current_path="brief.md",
            status=ResourceStatus.CURRENT,
            state_version=0,
            created_at=NOW,
            updated_at=NOW,
        ),
        resource_version,
        WorkspaceEvent(
            id=event_id,
            workspace_id=workspace_id,
            sequence=1,
            kind=WorkspaceEventKind.RESOURCE_CREATED,
            occurred_at=NOW,
            resource_id=resource_id,
        ),
        Claim(
            id=uuid4(),
            workspace_id=workspace_id,
            author_id=identity_id,
            text="A durable, typed assertion.",
            confidence=0.5,
            created_at=NOW,
        ),
        AgentIdentity(
            id=identity_id,
            display_name="Indexer",
            client_name="katsi-mcp",
            created_at=NOW,
        ),
        CapabilityGrant(
            id=uuid4(),
            identity_id=identity_id,
            workspace_id=workspace_id,
            operation_classes=frozenset({CapabilityOperationClass.CLAIM}),
            issued_at=NOW,
        ),
        WorkLease(
            id=uuid4(),
            workspace_id=workspace_id,
            holder_id=identity_id,
            kind=WorkLeaseKind.ADVISORY,
            task_description="Document contracts",
            acquired_at=NOW,
            expires_at=NOW.replace(hour=1),
        ),
        change_set,
        ChangeSetTransition(
            id=uuid4(),
            change_set_id=change_set_id,
            from_status=ChangeSetStatus.PROPOSED,
            to_status=ChangeSetStatus.VALIDATED,
            occurred_at=NOW,
        ),
        ActionOutcome(
            id=uuid4(),
            change_set_id=change_set_id,
            status=ActionOutcomeStatus.REJECTED,
            occurred_at=NOW,
        ),
    ]

    for model in models:
        assert type(model).model_validate_json(model.model_dump_json()) == model
    assert (
        TypeAdapter(Operation).validate_json(TypeAdapter(Operation).dump_json(operation))
        == operation
    )


def test_contracts_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Workspace(
            id=uuid4(),
            root_path="/projects/katsi",
            display_name="Katsi",
            state_version=0,
            created_at=NOW,
            updated_at=NOW,
            untrusted="ignored nowhere",
        )


def test_resource_version_is_immutable() -> None:
    version = ResourceVersion(
        id=uuid4(),
        resource_id=uuid4(),
        content_hash=HASH,
        byte_count=12,
        observed_at=NOW,
        source_event_id=uuid4(),
    )

    with pytest.raises(ValidationError, match="frozen_instance"):
        version.byte_count = 13


def test_changeset_operations_use_a_strict_discriminator() -> None:
    operation = TypeAdapter(CreateFileOperation).validate_python(
        {"path": "docs/brief.md", "byte_count": 10, "result_content_hash": HASH}
    )
    assert operation.kind == "create_file"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        TypeAdapter(CreateFileOperation).validate_python(
            {
                "path": "docs/brief.md",
                "byte_count": 10,
                "result_content_hash": HASH,
                "shell": "rm -rf .",
            }
        )


def test_changeset_rejects_invalid_transition() -> None:
    with pytest.raises(ValidationError, match="invalid Change Set transition"):
        ChangeSetTransition(
            id=uuid4(),
            change_set_id=uuid4(),
            from_status=ChangeSetStatus.PROPOSED,
            to_status=ChangeSetStatus.VERIFIED,
            occurred_at=NOW,
        )


def test_changeset_contract_accepts_valid_dependency_and_operation() -> None:
    change_set = ChangeSet(
        id=uuid4(),
        workspace_id=uuid4(),
        author_id=uuid4(),
        title="Add brief",
        idempotency_key="brief-v1",
        dependencies=(ResourceDependency(resource_id=uuid4(), expected_version_id=uuid4()),),
        operations=(CreateFileOperation(path="brief.md", byte_count=1, result_content_hash=HASH),),
        risk=RiskClass.LOW,
        created_at=NOW,
    )

    assert change_set.status is ChangeSetStatus.PROPOSED


def test_workspace_configuration_exposes_all_runtime_controls() -> None:
    settings = Settings()

    assert settings.workspace.sqlite.busy_timeout_ms > 0
    assert settings.workspace.observer.debounce_seconds >= 0
    assert settings.workspace.leases.advisory_ttl_seconds > 0
    assert settings.workspace.operations.max_operations > 0
    assert settings.workspace.recovery.retention_days >= 0
    assert settings.workspace.projection_worker.batch_size > 0


def test_verifier_configuration_validates_limits() -> None:
    with pytest.raises(ValidationError):
        VerifierDefinitionSettings(id="test", version="1", executable="pytest", timeout_seconds=0)


def test_event_kind_is_serializable() -> None:
    assert WorkspaceEventKind.EXTERNAL_CHANGE.value == "external_change"
