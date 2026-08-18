"""Tests for Task 7.6: Full Kùzu and LanceDB rebuilds from authoritative resources.

These tests verify that full rebuilds:
- Source from authoritative resources (current state)
- Use cached enrichment (avoid redundant LLM calls)
- Are idempotent and safe to run multiple times
"""

from __future__ import annotations

from pathlib import Path

import pytest

from katsi_core.models import Chunk
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore


@pytest.fixture
def vector_store(tmp_path: Path) -> VectorStore:
    """Create a vector store for testing."""
    vectors = VectorStore(tmp_path / "vectors")
    vectors.init_table(embed_dim=4)
    return vectors


@pytest.fixture
def graph_store(tmp_path: Path) -> GraphStore:
    """Create a graph store for testing."""
    return GraphStore(tmp_path / "graph")


@pytest.fixture
def sample_resources() -> list[tuple[str, str, str, str | None]]:
    """Create sample resource data for rebuild testing."""
    return [
        ("file1", "/path/to/file1.md", "file1.md", "Summary 1"),
        ("file2", "/path/to/file2.md", "file2.md", "Summary 2"),
        ("file3", "/path/to/file3.py", "file3.py", None),
    ]


@pytest.fixture
def sample_chunks() -> list[tuple[str, str, int, str, int]]:
    """Create sample chunk data for vector rebuild testing."""
    return [
        ("file1:0", "file1", 0, "Content from file 1 chunk 0", 5),
        ("file1:1", "file1", 1, "Content from file 1 chunk 1", 5),
        ("file2:0", "file2", 0, "Content from file 2", 4),
        ("file3:0", "file3", 0, "Content from file 3", 3),
    ]


@pytest.fixture
def sample_vectors() -> list[tuple[str, list[float]]]:
    """Create sample embedding data for vector rebuild testing."""
    return [
        ("file1:0", [1.0, 0.0, 0.0, 0.0]),
        ("file1:1", [0.0, 1.0, 0.0, 0.0]),
        ("file2:0", [0.0, 0.0, 1.0, 0.0]),
        ("file3:0", [0.0, 0.0, 0.0, 1.0]),
    ]


@pytest.fixture
def sample_entities() -> list[tuple[str, str, str]]:
    """Create sample entity data for graph rebuild testing."""
    return [
        ("file1", "Entity1", "person"),
        ("file1", "Entity2", "organization"),
        ("file2", "Entity1", "person"),
        ("file2", "Entity3", "location"),
    ]


@pytest.fixture
def sample_topics() -> list[tuple[str, str]]:
    """Create sample topic data for graph rebuild testing."""
    return [
        ("file1", "topic1"),
        ("file1", "topic2"),
        ("file2", "topic1"),
        ("file3", "topic3"),
    ]


@pytest.fixture
def sample_references() -> list[tuple[str, str]]:
    """Create sample reference data for graph rebuild testing."""
    return [
        ("file1", "file2"),
        ("file2", "file3"),
    ]


@pytest.fixture
def sample_duplicates() -> list[tuple[str, str, float]]:
    """Create sample duplicate data for graph rebuild testing."""
    return [
        ("file1", "file3", 0.85),
    ]


class TestVectorStoreRebuild:
    """Test vector store rebuild functionality."""

    def test_rebuild_builds_from_authoritative_chunks(
        self, vector_store: VectorStore, sample_chunks, sample_vectors
    ):
        """Rebuild constructs vector projection from authoritative chunks."""
        # Perform rebuild
        vector_store.rebuild_from_authoritative(sample_chunks, sample_vectors)

        # Verify all chunks are present
        assert vector_store.count() == 4

    def test_rebuild_uses_cached_embeddings(
        self, vector_store: VectorStore, sample_chunks, sample_vectors
    ):
        """Rebuild uses cached embeddings avoiding redundant LLM calls."""
        # Perform rebuild with cached embeddings
        vector_store.rebuild_from_authoritative(sample_chunks, sample_vectors)

        # Verify search works with cached embeddings
        results = vector_store.search([1.0, 0.0, 0.0, 0.0], k=5)
        assert len(results) >= 1
        # Find the best match (highest score)
        best_match = max(results, key=lambda x: x[2])
        assert best_match[0] == "file1:0"

        # Verify different embedding returns different result
        results = vector_store.search([0.0, 0.0, 1.0, 0.0], k=5)
        assert len(results) >= 1
        best_match = max(results, key=lambda x: x[2])
        assert best_match[0] == "file2:0"

    def test_rebuild_idempotent(self, vector_store: VectorStore, sample_chunks, sample_vectors):
        """Rebuild is idempotent and safe to run multiple times."""
        # First rebuild
        vector_store.rebuild_from_authoritative(sample_chunks, sample_vectors)
        first_count = vector_store.count()

        # Second rebuild with same data
        vector_store.rebuild_from_authoritative(sample_chunks, sample_vectors)
        second_count = vector_store.count()

        # Should be identical
        assert first_count == second_count == 4

        # Third rebuild
        vector_store.rebuild_from_authoritative(sample_chunks, sample_vectors)
        third_count = vector_store.count()

        assert third_count == 4

    def test_rebuild_clears_previous_data(
        self, vector_store: VectorStore, sample_chunks, sample_vectors
    ):
        """Rebuild clears previous data before rebuilding."""
        # Add some initial data using normal upsert
        initial_chunks = [
            Chunk(id="old:0", file_id="old", ordinal=0, text="old data", token_count=2)
        ]
        vector_store.upsert_chunks(initial_chunks, [[0.5, 0.5, 0.5, 0.5]])
        assert vector_store.count() == 1

        # Rebuild with new data
        vector_store.rebuild_from_authoritative(sample_chunks, sample_vectors)

        # Old data should be gone
        assert vector_store.count() == 4
        results = vector_store.search([0.5, 0.5, 0.5, 0.5], k=10)
        # Old chunk should not be found (all results should be from new data)
        chunk_ids = [r[0] for r in results]
        assert "old:0" not in chunk_ids

    def test_rebuild_handles_empty_data(self, vector_store: VectorStore):
        """Rebuild handles empty data gracefully."""
        # Add some data
        chunks = [Chunk(id="test:0", file_id="test", ordinal=0, text="test", token_count=1)]
        vector_store.upsert_chunks(chunks, [[1.0, 0.0, 0.0, 0.0]])
        assert vector_store.count() == 1

        # Rebuild with empty data
        vector_store.rebuild_from_authoritative([], [])

        # Should clear all data
        assert vector_store.count() == 0

    def test_rebuild_handles_missing_embeddings(
        self, vector_store: VectorStore, sample_chunks, sample_vectors
    ):
        """Rebuild handles chunks without corresponding embeddings."""
        # Remove one embedding
        incomplete_vectors = sample_vectors[:2]

        # Rebuild with incomplete embeddings
        vector_store.rebuild_from_authoritative(sample_chunks, incomplete_vectors)

        # Should only rebuild chunks with embeddings
        assert vector_store.count() == 2

    def test_rebuild_maintains_search_correctness(
        self, vector_store: VectorStore, sample_chunks, sample_vectors
    ):
        """Rebuild maintains search functionality correctness."""
        # Perform rebuild
        vector_store.rebuild_from_authoritative(sample_chunks, sample_vectors)

        # Test each embedding can find its corresponding chunk
        for chunk_id, vector in sample_vectors:
            results = vector_store.search(vector, k=5)
            assert len(results) >= 1
            found_ids = [r[0] for r in results]
            assert chunk_id in found_ids


class TestGraphStoreRebuild:
    """Test graph store rebuild functionality."""

    def test_rebuild_builds_from_authoritative_resources(
        self,
        graph_store: GraphStore,
        sample_resources,
        sample_entities,
        sample_topics,
        sample_references,
        sample_duplicates,
    ):
        """Rebuild constructs graph projection from authoritative resources and cached enrichment."""
        # Perform rebuild
        graph_store.rebuild_from_authoritative(
            resources=sample_resources,
            entities=sample_entities,
            topics=sample_topics,
            references=sample_references,
            duplicate_of=sample_duplicates,
        )

        # Verify all File nodes created
        for file_id, path, _name, _summary in sample_resources:
            file_node = graph_store.get_file(file_id)
            assert file_node is not None
            assert file_node.id == file_id
            assert file_node.path == path

    def test_rebuild_uses_cached_enrichment(
        self,
        graph_store: GraphStore,
        sample_resources,
        sample_entities,
        sample_topics,
        sample_references,
        sample_duplicates,
    ):
        """Rebuild uses cached enrichment avoiding redundant LLM calls."""
        # Perform rebuild with cached enrichment
        graph_store.rebuild_from_authoritative(
            resources=sample_resources,
            entities=sample_entities,
            topics=sample_topics,
            references=sample_references,
            duplicate_of=sample_duplicates,
        )

        # Verify entities were created from cache
        counts = graph_store.count_nodes()
        assert counts["entities"] == 3  # Entity1, Entity2, Entity3
        assert counts["topics"] == 3  # topic1, topic2, topic3

        # Verify relationships were created from cache
        file1_neighbors = graph_store.neighbors("file1")
        assert len(file1_neighbors) > 0

    def test_rebuild_idempotent(
        self,
        graph_store: GraphStore,
        sample_resources,
        sample_entities,
        sample_topics,
        sample_references,
        sample_duplicates,
    ):
        """Rebuild is idempotent and safe to run multiple times."""
        # First rebuild
        graph_store.rebuild_from_authoritative(
            resources=sample_resources,
            entities=sample_entities,
            topics=sample_topics,
            references=sample_references,
            duplicate_of=sample_duplicates,
        )

        first_counts = graph_store.count_nodes()

        # Second rebuild with same data
        graph_store.rebuild_from_authoritative(
            resources=sample_resources,
            entities=sample_entities,
            topics=sample_topics,
            references=sample_references,
            duplicate_of=sample_duplicates,
        )

        second_counts = graph_store.count_nodes()

        # Should be identical
        assert first_counts["entities"] == second_counts["entities"]
        assert first_counts["topics"] == second_counts["topics"]

        # Third rebuild
        graph_store.rebuild_from_authoritative(
            resources=sample_resources,
            entities=sample_entities,
            topics=sample_topics,
            references=sample_references,
            duplicate_of=sample_duplicates,
        )

        third_counts = graph_store.count_nodes()
        assert third_counts["entities"] == first_counts["entities"]

    def test_rebuild_clears_previous_data(
        self,
        graph_store: GraphStore,
        sample_resources,
        sample_entities,
        sample_topics,
    ):
        """Rebuild clears previous data before rebuilding."""
        # Add some initial data
        graph_store.upsert_entity("OldEntity", "test")
        graph_store.upsert_topic("old_topic")

        # Rebuild with new data
        graph_store.rebuild_from_authoritative(
            resources=sample_resources,
            entities=sample_entities,
            topics=sample_topics,
            references=[],
            duplicate_of=[],
        )

        # Verify old data is gone
        file_node = graph_store.get_file("file1")
        assert file_node is not None

        neighbors = graph_store.neighbors("file1")
        neighbor_names = [n.get("name") for n in neighbors if n.get("name")]

        assert "OldEntity" not in neighbor_names
        assert "old_topic" not in neighbor_names

    def test_rebuild_handles_empty_data(self, graph_store: GraphStore):
        """Rebuild handles empty data gracefully."""
        # Add some data
        graph_store.upsert_entity("Entity", "test")
        graph_store.upsert_topic("topic")

        # Rebuild with empty data
        graph_store.rebuild_from_authoritative(
            resources=[],
            entities=[],
            topics=[],
            references=[],
            duplicate_of=[],
        )

        # Should clear all data
        counts = graph_store.count_nodes()
        assert counts["entities"] == 0
        assert counts["topics"] == 0

    def test_rebuild_maintains_relationship_integrity(
        self,
        graph_store: GraphStore,
        sample_resources,
        sample_entities,
        sample_topics,
        sample_references,
    ):
        """Rebuild maintains relationship integrity and correctness."""
        # Perform rebuild
        graph_store.rebuild_from_authoritative(
            resources=sample_resources,
            entities=sample_entities,
            topics=sample_topics,
            references=sample_references,
            duplicate_of=[],
        )

        # Verify shared entity relationship
        file1_neighbors = graph_store.neighbors("file1")
        file2_neighbors = graph_store.neighbors("file2")

        # Both should share Entity1
        file1_entity_names = [
            n.get("name") for n in file1_neighbors if n.get("via") == "mentioned-entity"
        ]
        file2_entity_names = [
            n.get("name") for n in file2_neighbors if n.get("via") == "mentioned-entity"
        ]

        assert "Entity1" in file1_entity_names
        assert "Entity1" in file2_entity_names

        # Verify shared topic relationship
        file1_topic_names = [
            n.get("name") for n in file1_neighbors if n.get("via") == "shared-topic"
        ]
        file2_topic_names = [
            n.get("name") for n in file2_neighbors if n.get("via") == "shared-topic"
        ]

        assert "topic1" in file1_topic_names
        assert "topic1" in file2_topic_names

        # Verify reference relationship
        file1_refs = [n for n in file1_neighbors if n.get("via") == "references"]
        assert len(file1_refs) == 1
        assert file1_refs[0]["file_id"] == "file2"


class TestRebuildIntegration:
    """Test integrated rebuild scenarios."""

    def test_both_projections_rebuild_consistently(
        self,
        tmp_path: Path,
        sample_chunks,
        sample_vectors,
        sample_resources,
        sample_entities,
        sample_topics,
    ):
        """Both vector and graph projections rebuild consistently from same authoritative source."""
        # Create stores
        vectors = VectorStore(tmp_path / "vectors")
        vectors.init_table(embed_dim=4)
        graph = GraphStore(tmp_path / "graph")

        # Rebuild both projections
        vectors.rebuild_from_authoritative(sample_chunks, sample_vectors)
        graph.rebuild_from_authoritative(
            resources=sample_resources,
            entities=sample_entities,
            topics=sample_topics,
            references=[],
            duplicate_of=[],
        )

        # Verify consistency: same files exist in both
        vector_file_ids = set()
        results = vectors.search([1.0, 0.0, 0.0, 0.0], k=10)
        for _chunk_id, file_id, _score in results:
            vector_file_ids.add(file_id)

        # Check that vector file IDs exist in graph
        for file_id in vector_file_ids:
            file_node = graph.get_file(file_id)
            assert file_node is not None

    def test_rebuild_after_corruption_recovery(
        self,
        vector_store: VectorStore,
        graph_store: GraphStore,
        sample_chunks,
        sample_vectors,
        sample_resources,
        sample_entities,
        sample_topics,
    ):
        """Rebuild can recover from corrupted projection state."""
        # Perform initial rebuild
        vector_store.rebuild_from_authoritative(sample_chunks, sample_vectors)
        graph_store.rebuild_from_authoritative(
            resources=sample_resources,
            entities=sample_entities,
            topics=sample_topics,
            references=[],
            duplicate_of=[],
        )

        # Simulate corruption by manually adding bad data
        bad_chunks = [Chunk(id="bad:0", file_id="bad", ordinal=0, text="corrupted", token_count=1)]
        vector_store.upsert_chunks(bad_chunks, [[0.9, 0.1, 0.0, 0.0]])

        # Verify corruption exists
        assert vector_store.count() > 4

        # Rebuild to recover
        vector_store.rebuild_from_authoritative(sample_chunks, sample_vectors)

        # Verify corruption removed
        assert vector_store.count() == 4

        # Search for bad embedding should return nothing
        results = vector_store.search([0.9, 0.1, 0.0, 0.0], k=10)
        bad_results = [r for r in results if r[0] == "bad:0"]
        assert len(bad_results) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
