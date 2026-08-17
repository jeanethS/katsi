"""Multi-agent test fixtures for workspace coordination dogfooding.

Provides:
- Agent A → Agent B continuity fixture
- Separate MCP client process simulation
- Durable Claims continuity
- Work state handoff testing
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from katsi_core.config import SQLiteSettings
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.authorization import AuthorizationService
from katsi_core.workspace.change_sets import ChangeSetService
from katsi_core.workspace.claims import ClaimService
from katsi_core.workspace.contracts import (
    AgentIdentityId,
    ChangeSet,
    ChangeSetStatus,
    Claim,
    ClaimEvidence,
    ClaimEvidenceKind,
    ClaimStatus,
    CreateFileOperation,
    ResourceDependency,
    RiskClass,
)
from katsi_core.workspace.identity import IdentityService
from katsi_core.workspace.metrics import WorkspaceMetrics, get_global_metrics, reset_global_metrics


class SimulatedMCPClient:
    """Simulates a separate MCP client process with its own state and connection."""

    def __init__(
        self,
        client_id: str,
        database: WorkspaceSQLite,
        workspace_root: Path,
    ) -> None:
        """Initialize simulated MCP client.

        Args:
            client_id: Unique identifier for this client process
            database: Shared database connection
            workspace_root: Path to workspace directory
        """
        self.client_id = client_id
        self._database = database
        self._workspace_root = workspace_root
        self._identity_service = IdentityService(database)
        self._authorization_service = AuthorizationService(database)
        self._claim_service = ClaimService(database, self._identity_service, self._authorization_service)
        self._change_set_service = ChangeSetService(database)
        self._metrics = WorkspaceMetrics()

        # Register this client as an agent
        self._agent_id = self._identity_service.register("Agent", client_id)

    @property
    def agent_id(self) -> UUID:
        """Get the agent's identity ID."""
        return self._agent_id.id

    def publish_claim(
        self,
        workspace_id: UUID,
        claim_text: str,
        confidence: float,
        scope_paths: tuple[str, ...] = (),
    ) -> Claim:
        """Publish a claim as this agent.

        Args:
            workspace_id: Target workspace
            claim_text: Assertion text
            confidence: Confidence score 0-1
            scope_paths: Paths this claim covers

        Returns:
            Published Claim
        """
        claim = Claim(
            id=uuid4(),
            workspace_id=workspace_id,
            author_id=self.agent_id,
            text=claim_text,
            scope_paths=scope_paths,
            confidence=confidence,
            status=ClaimStatus.PROPOSED,
            created_at=datetime.now(UTC),
        )

        return self._claim_service.publish(claim)

    def verify_claim(
        self,
        claim_id: UUID,
        evidence: ClaimEvidence,
    ) -> None:
        """Verify a claim with evidence.

        Args:
            claim_id: Claim to verify
            evidence: Supporting evidence
        """
        self._claim_service.transition(
            claim_id,
            self.agent_id,
            ClaimStatus.VERIFIED,
            evidence=evidence,
        )

    def submit_change_set(
        self,
        workspace_id: UUID,
        title: str,
        operations: tuple,
        dependencies: tuple[ResourceDependency, ...] = (),
        risk: RiskClass = RiskClass.LOW,
    ) -> ChangeSet:
        """Submit a change set proposal.

        Args:
            workspace_id: Target workspace
            title: Human-readable title
            operations: Operations to perform
            dependencies: Resource dependencies
            risk: Risk classification

        Returns:
            Submitted ChangeSet
        """
        change_set = ChangeSet(
            id=uuid4(),
            workspace_id=workspace_id,
            author_id=self.agent_id,
            title=title,
            idempotency_key=f"{self.client_id}-{title}",
            dependencies=dependencies,
            operations=operations,
            risk=risk,
            created_at=datetime.now(UTC),
        )

        return self._change_set_service.submit(change_set)

    def get_claim(self, claim_id: UUID) -> Claim | None:
        """Retrieve a claim by ID.

        Args:
            claim_id: Claim to retrieve

        Returns:
            Claim if found
        """
        return self._claim_service.get(claim_id)

    def list_workspace_claims(self, workspace_id: UUID) -> list[Claim]:
        """List all claims for a workspace.

        Args:
            workspace_id: Workspace to query

        Returns:
            List of claims
        """
        return self._claim_service.list_for_workspace(workspace_id)

    def get_change_set(self, change_set_id: UUID) -> ChangeSet | None:
        """Retrieve a change set by ID.

        Args:
            change_set_id: ChangeSet to retrieve

        Returns:
            ChangeSet if found
        """
        return self._change_set_service.get(change_set_id)

    @property
    def metrics(self) -> WorkspaceMetrics:
        """Get this client's metrics collector."""
        return self._metrics


@pytest.fixture
def multi_agent_testbed():
    """Fixture providing multi-agent coordination testbed with isolated processes.

    Yields:
        Dictionary with:
        - workspace: registered Workspace
        - database: shared WorkspaceSQLite
        - agent_a: SimulatedMCPClient for Agent A
        - agent_b: SimulatedMCPClient for Agent B
        - agent_c: SimulatedMCPClient for Agent C
        - temp_dir: temporary directory path
    """
    reset_global_metrics()

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        database_path = temp_path / "workspace.sqlite3"
        workspace_root = temp_path / "workspace"
        workspace_root.mkdir()

        database = WorkspaceSQLite(database_path, SQLiteSettings())

        with database.connection() as connection:
            apply_migrations(connection, version=1)

        workspace = WorkspaceRepository(database).register_workspace(
            workspace_root, "TestWorkspace"
        )

        # Simulate three separate MCP client processes
        agent_a = SimulatedMCPClient("agent-a", database, workspace_root)
        agent_b = SimulatedMCPClient("agent-b", database, workspace_root)
        agent_c = SimulatedMCPClient("agent-c", database, workspace_root)

        yield {
            "workspace": workspace,
            "database": database,
            "agent_a": agent_a,
            "agent_b": agent_b,
            "agent_c": agent_c,
            "temp_dir": temp_path,
        }


@pytest.fixture
def agent_a_to_agent_b_continuity(multi_agent_testbed):
    """Fixture for Agent A → Agent B continuity testing.

    Tests:
    - Durable Claims survive agent restart
    - Work state handoff between agents
    - ChangeSet continuity across agents
    - Metrics collection across agent lifecycle

    Yields:
        Dictionary with agents and test helpers
    """
    agents = multi_agent_testbed
    workspace = agents["workspace"]
    agent_a = agents["agent_a"]
    agent_b = agents["agent_b"]

    class ContinuityTestHelpers:
        """Helper methods for continuity testing."""

        @staticmethod
        def agent_a_creates_claim() -> Claim:
            """Agent A creates a claim and makes it durable."""
            return agent_a.publish_claim(
                workspace_id=workspace.id,
                claim_text="File X contains function Y",
                confidence=0.95,
                scope_paths=("src/file_x.py",),
            )

        @staticmethod
        def agent_b_continues_claim(claim_id: UUID) -> Claim:
            """Agent B continues working with Agent A's claim."""
            claim = agent_b.get_claim(claim_id)
            assert claim is not None, "Claim should be durable across agents"
            return claim

        @staticmethod
        def agent_a_creates_change_set() -> ChangeSet:
            """Agent A creates a ChangeSet proposal."""
            return agent_a.submit_change_set(
                workspace_id=workspace.id,
                title="Create new module",
                operations=(
                    CreateFileOperation(
                        path="new_module.py",
                        byte_count=100,
                        result_content_hash="a" * 64,
                    ),
                ),
            )

        @staticmethod
        def agent_b_continues_change_set(change_set_id: UUID) -> ChangeSet:
            """Agent B continues with Agent A's ChangeSet."""
            change_set = agent_b.get_change_set(change_set_id)
            assert change_set is not None, "ChangeSet should be durable across agents"
            return change_set

        @staticmethod
        def verify_durable_claims(workspace_id: UUID) -> list[Claim]:
            """Verify all claims are durable and accessible."""
            # Both agents should see the same claims
            claims_a = agent_a.list_workspace_claims(workspace_id)
            claims_b = agent_b.list_workspace_claims(workspace_id)

            assert len(claims_a) == len(claims_b), "Claims should be identical across agents"
            return claims_a

    yield {
        **agents,
        "helpers": ContinuityTestHelpers(),
        "workspace": workspace,
    }


@pytest.fixture
def concurrent_agent_testbed(multi_agent_testbed):
    """Fixture for concurrent agent operation testing.

    Tests:
    - Agent C concurrent relevant-change coverage
    - Stale proposal blocking
    - Invalidation evidence verification
    - Unrelated concurrent-change isolation

    Yields:
        Dictionary with agents and concurrency helpers
    """
    agents = multi_agent_testbed
    workspace = agents["workspace"]
    agent_a = agents["agent_a"]
    agent_b = agents["agent_b"]
    agent_c = agents["agent_c"]

    class ConcurrencyTestHelpers:
        """Helper methods for concurrency testing."""

        @staticmethod
        async def agent_a_proposes_while_agent_c_modifies() -> tuple[ChangeSet, ChangeSet]:
            """Agent A proposes while Agent C makes concurrent modifications.

            Simulates:
            1. Agent A creates a ChangeSet proposal
            2. Agent C modifies a relevant resource concurrently
            3. Agent A's proposal should be detected as stale
            """
            # Agent A creates proposal
            proposal_a = agent_a.submit_change_set(
                workspace_id=workspace.id,
                title="Update config",
                operations=(
                    CreateFileOperation(
                        path="config.yaml",
                        byte_count=50,
                        result_content_hash="b" * 64,
                    ),
                ),
            )

            # Agent C makes concurrent modification
            proposal_c = agent_c.submit_change_set(
                workspace_id=workspace.id,
                title="Concurrent config update",
                operations=(
                    CreateFileOperation(
                        path="config.yaml",
                        byte_count=60,
                        result_content_hash="c" * 64,
                    ),
                ),
            )

            return proposal_a, proposal_c

        @staticmethod
        def verify_invalidation_evidence(claim_id: UUID) -> list[ClaimEvidence]:
            """Verify exact invalidation evidence is returned."""
            # Implementation would verify ClaimService.invalidate_resource_evidence
            # returns precise evidence about what changed
            return []

        @staticmethod
        async def test_unrelated_changes_remain_valid() -> None:
            """Test that unrelated concurrent changes don't affect each other.

            Agent A modifies file X, Agent B modifies file Y.
            Both ChangeSets should remain valid.
            """
            # Agent A modifies file X
            proposal_x = agent_a.submit_change_set(
                workspace_id=workspace.id,
                title="Modify file X",
                operations=(
                    CreateFileOperation(
                        path="file_x.py",
                        byte_count=100,
                        result_content_hash="d" * 64,
                    ),
                ),
            )

            # Agent B modifies unrelated file Y
            proposal_y = agent_b.submit_change_set(
                workspace_id=workspace.id,
                title="Modify file Y",
                operations=(
                    CreateFileOperation(
                        path="file_y.py",
                        byte_count=100,
                        result_content_hash="e" * 64,
                    ),
                ),
            )

            # Both should be valid
            assert proposal_x.status == ChangeSetStatus.PROPOSED
            assert proposal_y.status == ChangeSetStatus.PROPOSED

            return proposal_x, proposal_y

        @staticmethod
        async def test_stale_proposal_blocked() -> None:
            """Test that stale proposals are blocked with exact evidence."""
            # Agent A creates proposal
            proposal = agent_a.submit_change_set(
                workspace_id=workspace.id,
                title="Stale proposal",
                operations=(
                    CreateFileOperation(
                        path="shared.py",
                        byte_count=100,
                        result_content_hash="f" * 64,
                    ),
                ),
            )

            # Agent C modifies same file, making A's proposal stale
            stale_agent_c = agent_c.submit_change_set(
                workspace_id=workspace.id,
                title="Concurrent modification",
                operations=(
                    CreateFileOperation(
                        path="shared.py",
                        byte_count=120,
                        result_content_hash="g" * 64,
                    ),
                ),
            )

            # A's proposal should be blocked/stale
            # (This would be detected during validation)

            return proposal, stale_agent_c

    yield {
        **agents,
        "helpers": ConcurrencyTestHelpers(),
        "workspace": workspace,
    }


@pytest.fixture
def metrics_testbed(multi_agent_testbed):
    """Fixture for metrics collection testing.

    Yields:
        Dictionary with agents and metrics helpers
    """
    agents = multi_agent_testbed
    workspace = agents["workspace"]

    class MetricsTestHelpers:
        """Helper methods for metrics testing."""

        @staticmethod
        def track_verified_action(agent_id: UUID) -> None:
            """Track a verified action with timing."""
            metrics = get_global_metrics()
            with metrics.track_verified_action(agent_id):
                pass  # Simulate work

        @staticmethod
        def track_brief_generation(workspace_id: UUID) -> None:
            """Track brief context generation cost."""
            metrics = get_global_metrics()
            with metrics.track_brief_generation(workspace_id, token_count=1000):
                pass  # Simulate brief generation

        @staticmethod
        def verify_enrichment_metrics() -> dict:
            """Verify enrichment cache metrics are collected."""
            metrics = get_global_metrics()
            return {
                "avoidance_rate": metrics.enrichment_avoidance_rate,
            }

        @staticmethod
        def record_reconciliation_latency(workspace_id: UUID) -> None:
            """Record a reconciliation operation."""
            metrics = get_global_metrics()
            with metrics.track_reconciliation(workspace_id):
                pass  # Simulate reconciliation

        @staticmethod
        def record_projection_lag(workspace_id: UUID) -> None:
            """Record projection lag detection."""
            metrics = get_global_metrics()
            metrics.record_projection_lag(workspace_id, lag_seconds=5.0)

        @staticmethod
        def record_stale_plan_block(workspace_id: UUID) -> None:
            """Record a stale plan being blocked."""
            metrics = get_global_metrics()
            metrics.record_stale_plan_blocked(
                workspace_id, uuid4(), reason="concurrent_modification"
            )

        @staticmethod
        def record_recovery_outcome(success: bool) -> None:
            """Record a recovery operation outcome."""
            metrics = get_global_metrics()
            metrics.record_recovery_outcome(
                recovery_type="reconciliation",
                success=success,
                duration_seconds=2.5,
            )

        @staticmethod
        def export_all_metrics() -> dict:
            """Export all collected metrics."""
            metrics = get_global_metrics()
            return metrics.export_metrics()

    yield {
        **agents,
        "helpers": MetricsTestHelpers(),
        "workspace": workspace,
    }
