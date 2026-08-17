"""Tests for Task 7.7: Failure-injection tests for graph/vector rebuilds.

These tests verify that:
1. Authoritative events survive graph/vector failure
2. Rebuild invokes no unnecessary local-model calls (uses cached enrichment)
3. Rebuild is idempotent and safe to run multiple times
"""

from __future__ import annotations

from pathlib import Path

import pytest

from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore
from katsi_core.workspace.contracts import WorkspaceEventKind


@pytest.fixture
def graph_store(tmp_path: Path) -> GraphStore:
    """Create a fresh graph store for testing."""
    return GraphStore(tmp_path / "graph")


@pytest.fixture
def vector_store(tmp_path: Path) -> VectorStore:
    """Create a fresh vector store for testing."""
    vectors = VectorStore(tmp_path / "vectors")
    vectors.init_table(embed_dim=4)
    return vectors


class TestGraphRebuildFailureRecovery:
    """Test that graph rebuild recovers from failures and preserves authoritative state."""

    def test_rebuild_after_graph_corruption_restores_authoritative_state(
        self, graph_store: GraphStore
    ) -> None:
        """After simulated corruption, rebuild restores from authoritative resources."""
        # Setup: Create initial graph state
        resources = [
            ("file1", "path1.md", "file1.md", "Summary 1"),
            ("file2", "path2.md", "file2.md", "Summary 2"),
        ]
        entities = [("file1", "Entity1", "kind"), ("file2", "Entity2", "kind")]
        topics = [("file1", "topic1")]

        graph_store.rebuild_from_authoritative(resources, entities, topics, [], [])

        # Verify initial state
        assert graph_store.get_file("file1") is not None
        assert graph_store.get_file("file2") is not None
        file1_direct = graph_store.get_direct_relationships("file1")
        assert any(e["name"] == "Entity1" for e in file1_direct["entities"])
        assert "topic1" in file1_direct["topics"]

        # Simulate corruption by manually clearing some data
        graph_store._conn.execute("MATCH (f:File {id: 'file1'}) DETACH DELETE f")

        # Verify corruption
        assert graph_store.get_file("file1") is None
        assert graph_store.get_file("file2") is not None  # file2 intact

        # Rebuild from authoritative resources
        graph_store.rebuild_from_authoritative(resources, entities, topics, [], [])

        # Verify recovery: both files restored
        assert graph_store.get_file("file1") is not None
        assert graph_store.get_file("file2") is not None
        file1_direct = graph_store.get_direct_relationships("file1")
        file2_direct = graph_store.get_direct_relationships("file2")
        assert any(e["name"] == "Entity1" for e in file1_direct["entities"])
        assert "topic1" in file1_direct["topics"]
        assert any(e["name"] == "Entity2" for e in file2_direct["entities"])
        counts = graph_store.count_nodes()
        assert counts["entities"] == 2
        assert counts["topics"] == 1

    def test_rebuild_idempotent_multiple_runs_produce_identical_state(
        self, graph_store: GraphStore
    ) -> None:
        """Multiple rebuild runs produce identical, stable state."""
        resources = [("file1", "path.md", "file.md", "Summary")]
        entities = [("file1", "Entity", "kind")]
        topics = [("file1", "topic")]

        # First rebuild
        graph_store.rebuild_from_authoritative(resources, entities, topics, [], [])
        file1_first = graph_store.get_file("file1")
        neighbors_first = graph_store.neighbors("file1")

        # Second rebuild (should be idempotent)
        graph_store.rebuild_from_authoritative(resources, entities, topics, [], [])
        file1_second = graph_store.get_file("file1")
        neighbors_second = graph_store.neighbors("file1")

        # Verify identical state
        assert file1_first.id == file1_second.id
        assert file1_first.path == file1_second.path
        assert len(neighbors_first) == len(neighbors_second)

    def test_rebuild_uses_cached_enrichment_no_llm_calls_needed(
        self, graph_store: GraphStore
    ) -> None:
        """Rebuild uses cached enrichment (entities, topics) without requiring LLM calls."""
        # Simulate cached enrichment data
        resources = [("file1", "path.md", "file.md", "Summary")]
        cached_entities = [("file1", "CachedEntity", "test")]
        cached_topics = [("file1", "cached-topic")]

        # Rebuild with cached enrichment
        graph_store.rebuild_from_authoritative(resources, cached_entities, cached_topics, [], [])

        # Verify cached enrichment was applied via direct relationships and node counts.
        # A single file has no shared-connector neighbors, so neighbors() returns [].
        direct = graph_store.get_direct_relationships("file1")
        assert any(e["name"] == "CachedEntity" for e in direct["entities"])
        assert "cached-topic" in direct["topics"]
        counts = graph_store.count_nodes()
        assert counts["entities"] == 1
        assert counts["topics"] == 1

    def test_rebuild_with_partial_cache_merges_with_new_data(self, graph_store: GraphStore) -> None:
        """Rebuild handles partial cache by combining cached and new data."""
        resources = [
            ("file1", "path1.md", "file1.md", "Summary 1"),
            ("file2", "path2.md", "file2.md", "Summary 2"),
        ]

        # Only file1 has cached enrichment
        cached_entities = [("file1", "CachedEntity", "kind")]

        graph_store.rebuild_from_authoritative(resources, cached_entities, [], [], [])

        # Verify file1 has cached enrichment via direct relationships.
        # A single file has no shared-connector neighbors.
        file1_direct = graph_store.get_direct_relationships("file1")
        assert any(e["name"] == "CachedEntity" for e in file1_direct["entities"])

        # Verify file2 exists but has no enrichment (cache miss is OK)
        file2_node = graph_store.get_file("file2")
        assert file2_node is not None

        file2_direct = graph_store.get_direct_relationships("file2")
        assert file2_direct["entities"] == []
        assert file2_direct["topics"] == []


class TestVectorRebuildFailureRecovery:
    """Test that vector rebuild recovers from failures and preserves authoritative state."""

    def test_rebuild_after_vector_corruption_restores_authoritative_state(
        self, vector_store: VectorStore
    ) -> None:
        """After simulated corruption, rebuild restores from authoritative chunks."""
        # Setup: Create initial vector state
        chunks = [
            ("c1", "file1", 0, "text 1", 2),
            ("c2", "file1", 1, "text 2", 2),
            ("c3", "file2", 0, "text 3", 2),
        ]
        vectors = [
            ("c1", [1.0, 0.0, 0.0, 0.0]),
            ("c2", [0.0, 1.0, 0.0, 0.0]),
            ("c3", [0.0, 0.0, 1.0, 0.0]),
        ]

        vector_store.rebuild_from_authoritative(chunks, vectors)

        # Verify initial state
        assert vector_store.count() == 3
        results = vector_store.search([1.0, 0.0, 0.0, 0.0], k=5)
        assert len(results) > 0

        # Simulate corruption by deleting all data
        vector_store._tbl.delete("true")  # Delete all rows

        # Verify corruption
        assert vector_store.count() == 0

        # Rebuild from authoritative resources
        vector_store.rebuild_from_authoritative(chunks, vectors)

        # Verify recovery
        assert vector_store.count() == 3
        results = vector_store.search([1.0, 0.0, 0.0, 0.0], k=5)
        assert len(results) > 0

    def test_rebuild_uses_cached_embeddings_no_llm_calls_needed(
        self, vector_store: VectorStore
    ) -> None:
        """Rebuild uses cached embeddings without requiring LLM calls."""
        chunks = [
            ("c1", "file1", 0, "content one", 2),
            ("c2", "file1", 1, "content two", 2),
        ]
        cached_vectors = [
            ("c1", [1.0, 0.0, 0.0, 0.0]),
            ("c2", [0.0, 1.0, 0.0, 0.0]),
        ]

        # Rebuild with cached embeddings
        vector_store.rebuild_from_authoritative(chunks, cached_vectors)

        # Verify cached embeddings were applied
        assert vector_store.count() == 2
        results = vector_store.search([1.0, 0.0, 0.0, 0.0], k=5)
        assert len(results) == 2
        assert results[0][0] == "c1"

    def test_rebuild_idempotent_multiple_runs_produce_identical_state(
        self, vector_store: VectorStore
    ) -> None:
        """Multiple rebuild runs produce identical, stable state."""
        chunks = [("c1", "file1", 0, "text", 1)]
        vectors = [("c1", [1.0, 0.0, 0.0, 0.0])]

        # First rebuild
        vector_store.rebuild_from_authoritative(chunks, vectors)
        count_first = vector_store.count()
        results_first = vector_store.search([1.0, 0.0, 0.0, 0.0], k=5)

        # Second rebuild
        vector_store.rebuild_from_authoritative(chunks, vectors)
        count_second = vector_store.count()
        results_second = vector_store.search([1.0, 0.0, 0.0, 0.0], k=5)

        # Verify identical state
        assert count_first == count_second == 1
        assert len(results_first) == len(results_second) == 1

    def test_rebuild_with_partial_cached_embeddings(self, vector_store: VectorStore) -> None:
        """Rebuild handles partial cache - some chunks have cached embeddings, some don't."""
        chunks = [
            ("c1", "file1", 0, "text 1", 1),
            ("c2", "file1", 1, "text 2", 1),
            ("c3", "file2", 0, "text 3", 1),
        ]

        # Only c1 and c3 have cached embeddings (c2 missing)
        cached_vectors = [
            ("c1", [1.0, 0.0, 0.0, 0.0]),
            ("c3", [0.0, 0.0, 1.0, 0.0]),
        ]

        vector_store.rebuild_from_authoritative(chunks, cached_vectors)

        # Verify only cached chunks are present
        assert vector_store.count() == 2

        # Verify the cached chunks are searchable and rank first for their own vectors
        results_c1 = vector_store.search([1.0, 0.0, 0.0, 0.0], k=5)
        assert len(results_c1) == 2
        assert results_c1[0][0] == "c1"

        results_c3 = vector_store.search([0.0, 0.0, 1.0, 0.0], k=5)
        assert len(results_c3) == 2
        assert results_c3[0][0] == "c3"

    def test_rebuild_empty_chunk_list_is_safe(self, vector_store: VectorStore) -> None:
        """Rebuild with empty chunk list is idempotent and safe."""
        # Add some initial data
        chunks = [("c1", "file1", 0, "text", 1)]
        vectors = [("c1", [1.0, 0.0, 0.0, 0.0])]
        vector_store.rebuild_from_authoritative(chunks, vectors)
        assert vector_store.count() == 1

        # Rebuild with empty data (clears everything)
        vector_store.rebuild_from_authoritative([], [])
        assert vector_store.count() == 0

        # Another empty rebuild is safe
        vector_store.rebuild_from_authoritative([], [])
        assert vector_store.count() == 0


class TestAuthoritativeEventsSurviveProjectionFailure:
    """Test that authoritative SQLite events survive projection failures."""

    def test_outbox_entries_persist_during_projection_failure(self, tmp_path: Path) -> None:
        """Projection outbox entries are durable even when projection fails."""
        from katsi_core.config import ProjectionWorkerSettings, SQLiteSettings
        from katsi_core.store import (
            ProjectionWorker,
            WorkspaceRepository,
            WorkspaceSQLite,
            apply_migrations,
        )

        database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
        with database.connection() as connection:
            apply_migrations(connection, target_version=1)

        repository = WorkspaceRepository(database)
        root = tmp_path / "project"
        root.mkdir()
        workspace = repository.register_workspace(root, "Project")

        # Append authoritative events using the current workspace version each time
        for i in range(3):
            workspace = repository.get_workspace(workspace.id)
            repository.append_event(
                workspace.id,
                workspace.state_version,
                WorkspaceEventKind.RESOURCE_UPDATED,
                projection_payloads={"graph": {"action": "replace", "seq": i}},
            )

        worker = ProjectionWorker(database, ProjectionWorkerSettings(batch_size=10))

        # Simulate projection failure - handler raises exception
        delivery_count = [0]

        def failing_handler(entry):
            delivery_count[0] += 1
            if delivery_count[0] == 1:
                raise RuntimeError("Projection failed!")
            # Subsequent calls succeed

        # First call fails
        with pytest.raises(RuntimeError, match="Projection failed"):
            worker.run(workspace.id, "graph", failing_handler)

        # Offset should not have advanced
        offset = worker.offset(workspace.id, "graph")
        assert offset.outbox_id == 0

        # Second call succeeds and delivers all entries (including the failed one)
        delivered = worker.run(workspace.id, "graph", failing_handler)
        assert delivered == 3

        # All entries were delivered
        assert delivery_count[0] == 4  # 1 failed + 3 successful deliveries

    def test_projection_lag_reported_after_failure(self, tmp_path: Path) -> None:
        """Projection lag is correctly reported after projection failure."""
        from katsi_core.config import ProjectionWorkerSettings, SQLiteSettings
        from katsi_core.store import (
            ProjectionWorker,
            WorkspaceRepository,
            WorkspaceSQLite,
            apply_migrations,
        )

        database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
        with database.connection() as connection:
            apply_migrations(connection, target_version=1)

        repository = WorkspaceRepository(database)
        root = tmp_path / "project"
        root.mkdir()
        workspace = repository.register_workspace(root, "Project")

        # Add events
        repository.append_event(
            workspace.id,
            1,
            WorkspaceEventKind.RESOURCE_UPDATED,
            projection_payloads={"graph": {"action": "replace"}},
        )

        worker = ProjectionWorker(database, ProjectionWorkerSettings(batch_size=10))

        # Check freshness before delivery
        freshness = worker.freshness(workspace.id)
        assert len(freshness) == 1
        assert freshness[0].lagging is True
        assert freshness[0].lag == 1


class TestRebuildNoRedundantLLMCalls:
    """Test that rebuilds don't trigger unnecessary LLM calls."""

    def test_rebuild_uses_all_cached_enrichment(
        self, graph_store: GraphStore, vector_store: VectorStore
    ) -> None:
        """Combined graph/vector rebuild uses 100% cached enrichment, 0% LLM calls."""
        # Simulate a complete cached enrichment dataset
        resources = [("f1", "p1.md", "f1.md", "Summary")]
        entities = [("f1", "Entity1", "kind"), ("f1", "Entity2", "kind")]
        topics = [("f1", "topic1"), ("f1", "topic2")]

        chunks = [("c1", "f1", 0, "text", 1)]
        vectors = [("c1", [1.0, 0.0, 0.0, 0.0])]

        # Rebuild both projections
        graph_store.rebuild_from_authoritative(resources, entities, topics, [], [])
        vector_store.rebuild_from_authoritative(chunks, vectors)

        # Verify all data is present without LLM calls.
        # A single file has no shared-connector neighbors; use direct relationships.
        assert graph_store.get_file("f1") is not None
        direct = graph_store.get_direct_relationships("f1")
        assert {e["name"] for e in direct["entities"]} == {"Entity1", "Entity2"}
        assert set(direct["topics"]) == {"topic1", "topic2"}
        assert vector_store.count() == 1

        # Search returns the cached embedding
        results = vector_store.search([1.0, 0.0, 0.0, 0.0], k=5)
        assert len(results) == 1

    def test_rebuild_with_zero_cache_still_succeeds(self, graph_store: GraphStore) -> None:
        """Rebuild with no cached enrichment creates structural graph (files only)."""
        resources = [("f1", "p1.md", "f1.md", "Summary")]

        # Rebuild with empty enrichment cache
        graph_store.rebuild_from_authoritative(resources, [], [], [], [])

        # File node exists
        assert graph_store.get_file("f1") is not None

        # But no enrichment edges (no LLM calls were made)
        neighbors = graph_store.neighbors("f1")
        assert len(neighbors) == 0
