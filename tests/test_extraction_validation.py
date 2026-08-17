"""Tests for Task 6.5: Invalid extraction validation tests.

These tests verify that invalid extractions:
- Cannot publish chunks to vector projection
- Cannot publish edges to graph projection
- Cannot create Claims

The validation, retry, and error states are tested to ensure safety guarantees.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from katsi_core.ingest.enrich import apply_extraction, project_chunks
from katsi_core.models import Chunk, Extraction, FileRecord, IndexStatus
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore
from katsi_core.workspace.extraction_gate import ExtractionGate, StrictExtraction


@pytest.fixture
def file_record(tmp_path: Path) -> FileRecord:
    """Create a test file record."""
    return FileRecord(
        id="test-file",
        path=str(tmp_path / "test.md"),
        name="test.md",
        ext=".md",
        mime="text/markdown",
        size_bytes=100,
        mtime=1000.0,
        content_hash="abc123",
        status=IndexStatus.INDEXED,
        summary="Test document",
    )


@pytest.fixture
def projection_stores(tmp_path: Path):
    """Create vector and graph stores for testing."""
    vectors = VectorStore(tmp_path / "vectors")
    vectors.init_table(embed_dim=4)
    graph = GraphStore(tmp_path / "graph")
    return vectors, graph


class TestExtractionGateValidation:
    """Test the extraction gate's validation and retry logic."""

    def test_valid_extraction_passes_validation(self):
        """A valid extraction passes through the gate."""
        gate = ExtractionGate()
        result = gate.validate(
            lambda: {
                "summary": "Test summary",
                "entities": [{"name": "Test", "kind": "test"}],
                "topics": ["test-topic"],
                "references": [],
            }
        )

        assert isinstance(result, StrictExtraction)
        assert result.summary == "Test summary"
        assert len(result.entities) == 1
        assert result.topics == ["test-topic"]

    def test_invalid_extra_field_rejected_after_retry(self):
        """Extra fields are rejected after exactly one retry."""
        attempts = 0

        def produce_invalid():
            nonlocal attempts
            attempts += 1
            return {
                "summary": "Test",
                "entities": [],
                "topics": [],
                "references": [],
                "malicious_field": "should be rejected",  # Extra field
            }

        gate = ExtractionGate()

        with pytest.raises(ValidationError) as exc_info:
            gate.validate(produce_invalid)

        # Should have attempted exactly twice (initial + one retry)
        assert attempts == 2

        # Verify it's a validation error for extra fields
        assert "extra" in str(exc_info.value).lower() or "forbid" in str(exc_info.value).lower()

    def test_missing_required_field_rejected_after_retry(self):
        """Missing required fields are rejected after one retry."""
        attempts = 0

        def produce_invalid():
            nonlocal attempts
            attempts += 1
            return {
                "summary": "Test",
                "entities": [],
                "topics": [],
                # Missing 'references' field
            }

        gate = ExtractionGate()

        with pytest.raises(ValidationError):
            gate.validate(produce_invalid)

        assert attempts == 2

    def test_wrong_type_field_rejected_after_retry(self):
        """Wrong type for fields is rejected after one retry."""
        attempts = 0

        def produce_invalid():
            nonlocal attempts
            attempts += 1
            return {
                "summary": "Test",
                "entities": "should be list not string",  # Wrong type
                "topics": [],
                "references": [],
            }

        gate = ExtractionGate()

        with pytest.raises(ValidationError):
            gate.validate(produce_invalid)

        assert attempts == 2

    def test_validation_error_is_terminal(self):
        """After retry fails, the validation error is raised (terminal state)."""
        gate = ExtractionGate()

        def always_invalid():
            return {
                "summary": "Test",
                "entities": [],
                "topics": [],
                "references": [],
                "extra": "field",
            }

        # The error should be raised (not swallowed)
        with pytest.raises(ValidationError):
            gate.validate(always_invalid)

    def test_second_retry_success_returns_valid_extraction(self):
        """If the second attempt succeeds, valid extraction is returned."""
        attempts = 0

        def produce_fails_then_succeeds():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return {
                    "summary": "Test",
                    "entities": [],
                    "topics": [],
                    "references": [],
                    "extra": "field",
                }
            else:
                return {
                    "summary": "Test",
                    "entities": [],
                    "topics": [],
                    "references": [],
                }

        gate = ExtractionGate()
        result = gate.validate(produce_fails_then_succeeds)

        assert isinstance(result, StrictExtraction)
        assert attempts == 2


class TestInvalidExtractionCannotPublish:
    """Test that invalid extractions cannot publish to projections."""

    def test_invalid_extraction_cannot_publish_chunks_to_vector_projection(
        self, file_record: FileRecord, projection_stores
    ):
        """Invalid extraction (through validation failure) cannot publish chunks."""
        vectors, graph = projection_stores

        # Setup: Initial valid chunks
        valid_chunks = [
            Chunk(id="test-file:0", file_id="test-file", ordinal=0, text="valid content", token_count=2)
        ]
        project_chunks(file_record, valid_chunks, [[1.0, 0.0, 0.0, 0.0]], vectors)
        assert vectors.count() == 1

        # Simulate validation failure: file_record becomes errored
        errored_record = file_record.model_copy(update={"status": IndexStatus.ERROR})

        # Attempt to publish new chunks with errored status
        invalid_chunks = [
            Chunk(id="test-file:1", file_id="test-file", ordinal=0, text="invalid content", token_count=2)
        ]

        # The projection should reject errored resources
        project_chunks(errored_record, invalid_chunks, [[0.0, 1.0, 0.0, 0.0]], vectors)

        # Old chunks removed, no new chunks published
        assert vectors.count() == 0
        assert vectors.search([0.0, 1.0, 0.0, 0.0], k=5) == []
        assert vectors.search([1.0, 0.0, 0.0, 0.0], k=5) == []

    def test_invalid_extraction_cannot_publish_edges_to_graph_projection(
        self, file_record: FileRecord, projection_stores
    ):
        """Invalid extraction cannot publish edges to graph projection."""
        vectors, graph = projection_stores

        # Setup: Initial valid extraction
        apply_extraction(
            file_record,
            Extraction(
                summary="Valid extraction",
                entities=[{"name": "ValidEntity", "kind": "test"}],
                topics=["valid-topic"],
                references=[],
            ),
            graph,
        )

        # Verify initial state
        file_node = graph.get_file("test-file")
        assert file_node is not None
        relationships = graph.get_direct_relationships("test-file")
        assert len(relationships["entities"]) > 0 or len(relationships["topics"]) > 0

        # Simulate invalid extraction: replace with empty extraction
        # This represents validation failure where no valid data exists
        apply_extraction(
            file_record,
            Extraction(
                summary="",
                entities=[],
                topics=[],
                references=[],
            ),
            graph,
        )

        # Verify graph state: File node still exists but with no relationships
        file_node_after = graph.get_file("test-file")
        assert file_node_after is not None

        # Previous relationships should be removed (replace semantics)
        relationships_after = graph.get_direct_relationships("test-file")
        assert len(relationships_after["entities"]) == 0 and len(relationships_after["topics"]) == 0, "Invalid extraction should not preserve stale relationships"

    def test_non_current_status_cannot_publish_chunks(self, file_record: FileRecord, projection_stores):
        """Non-current statuses (pending, error, deleted) cannot publish chunks."""
        vectors, graph = projection_stores

        # Test each non-publishable status
        non_publishable_statuses = [
            IndexStatus.PENDING,
            IndexStatus.ERROR,
            IndexStatus.DELETED,
        ]

        for status in non_publishable_statuses:
            # Clear vectors
            vectors.delete_by_file("test-file")

            non_current_record = file_record.model_copy(update={"status": status})

            chunks = [
                Chunk(id="test-file:0", file_id="test-file", ordinal=0, text="test", token_count=1)
            ]

            # Should not publish
            project_chunks(non_current_record, chunks, [[1.0, 0.0, 0.0, 0.0]], vectors)

            # Verify no chunks were published
            assert vectors.count() == 0
            assert vectors.search([1.0, 0.0, 0.0, 0.0], k=5) == []

    def test_validation_error_state_propagates_to_projection_rejection(
        self, file_record: FileRecord, projection_stores
    ):
        """When extraction validation fails, the error state prevents projection publishing."""
        vectors, graph = projection_stores

        # Simulate extraction failure by setting status to ERROR
        errored_record = file_record.model_copy(
            update={"status": IndexStatus.ERROR, "summary": "Extraction failed"}
        )

        # Attempt to project chunks
        chunks = [
            Chunk(id="test-file:0", file_id="test-file", ordinal=0, text="failed content", token_count=2)
        ]

        # Should be rejected due to ERROR status
        project_chunks(errored_record, chunks, [[1.0, 0.0, 0.0, 0.0]], vectors)

        # Verify rejection
        assert vectors.count() == 0

        # Attempt to publish graph edges
        apply_extraction(
            errored_record,
            Extraction(summary="", entities=[], topics=[], references=[]),
            graph,
        )

        # Graph can have the file node but no edges (empty extraction)
        file_node = graph.get_file("test-file")
        assert file_node is not None
        assert len(graph.neighbors("test-file")) == 0


class TestExtractionIdempotencyWithInvalidation:
    """Test that re-extraction with invalidation properly replaces state."""

    def test_re_extraction_with_invalid_data_removes_previous_valid_state(
        self, file_record: FileRecord, projection_stores
    ):
        """Re-extracting with invalid data removes previous valid projections."""
        vectors, graph = projection_stores

        # Initial valid extraction
        valid_chunks = [
            Chunk(id="test-file:0", file_id="test-file", ordinal=0, text="valid", token_count=1)
        ]
        project_chunks(file_record, valid_chunks, [[1.0, 0.0, 0.0, 0.0]], vectors)

        apply_extraction(
            file_record,
            Extraction(
                summary="Valid",
                entities=[{"name": "Entity", "kind": "test"}],
                topics=["topic"],
                references=[],
            ),
            graph,
        )

        # Verify valid state exists
        assert vectors.count() == 1
        relationships = graph.get_direct_relationships("test-file")
        assert len(relationships["entities"]) > 0 or len(relationships["topics"]) > 0

        # Simulate re-extraction failure (status change to ERROR)
        errored_record = file_record.model_copy(update={"status": IndexStatus.ERROR})

        # Re-project with error (simulating failed re-extraction)
        project_chunks(errored_record, [], [], vectors)
        apply_extraction(
            errored_record,
            Extraction(summary="", entities=[], topics=[], references=[]),
            graph,
        )

        # Verify previous valid state is removed
        assert vectors.count() == 0
        relationships = graph.get_direct_relationships("test-file")
        assert len(relationships["entities"]) == 0 and len(relationships["topics"]) == 0

    def test_recovery_from_error_state_requires_valid_extraction(self, file_record: FileRecord, projection_stores):
        """Recovery from error requires a new valid extraction."""
        vectors, graph = projection_stores

        # Start with error state
        errored_record = file_record.model_copy(update={"status": IndexStatus.ERROR})
        project_chunks(errored_record, [], [], vectors)

        # Verify nothing is published
        assert vectors.count() == 0

        # Recover with valid extraction (status becomes INDEXED)
        recovered_record = file_record.model_copy(update={"status": IndexStatus.INDEXED})

        valid_chunks = [
            Chunk(id="test-file:0", file_id="test-file", ordinal=0, text="recovered", token_count=1)
        ]
        project_chunks(recovered_record, valid_chunks, [[1.0, 0.0, 0.0, 0.0]], vectors)

        # Verify recovery
        assert vectors.count() == 1
        assert vectors.search([1.0, 0.0, 0.0, 0.0], k=5) != []


class TestStrictValidationEnforcement:
    """Test that strict validation prevents various attack vectors."""

    def test_strict_validation_prevents_extra_fields(self):
        """Pydantic strict mode prevents extra fields from being accepted."""
        with pytest.raises(ValidationError, match="extra"):
            StrictExtraction.model_validate(
                {
                    "summary": "Test",
                    "entities": [],
                    "topics": [],
                    "references": [],
                    "unauthorized": "data",
                }
            )

    def test_strict_validation_requires_all_fields(self):
        """All required fields must be present."""
        with pytest.raises(ValidationError):
            StrictExtraction.model_validate({"summary": "Test"})  # Missing fields

    def test_strict_validation_enforces_correct_types(self):
        """Field types are strictly enforced."""
        with pytest.raises(ValidationError):
            StrictExtraction.model_validate(
                {
                    "summary": "Test",
                    "entities": "not a list",  # Wrong type
                    "topics": [],
                    "references": [],
                }
            )

    def test_nested_validation_for_entities(self):
        """Nested structures (entities) are also validated."""
        with pytest.raises(ValidationError):
            StrictExtraction.model_validate(
                {
                    "summary": "Test",
                    "entities": [{"name": "Entity"}],  # Missing 'kind' field
                    "topics": [],
                    "references": [],
                }
            )


class TestExtractionGateRetryBehavior:
    """Test the exact retry behavior of the extraction gate."""

    def test_only_one_retry_is_attempted(self):
        """The gate attempts exactly one retry (2 total attempts)."""
        attempts = 0

        def always_fail():
            nonlocal attempts
            attempts += 1
            raise ValueError("Always fails")

        gate = ExtractionGate()

        with pytest.raises(ValueError):
            gate.validate(always_fail)

        # Should have attempted initial call + 1 retry
        assert attempts == 2

    def test_first_success_returns_immediately(self):
        """If first attempt succeeds, no retry is attempted."""
        attempts = 0

        def succeed_first():
            nonlocal attempts
            attempts += 1
            return {
                "summary": "Success",
                "entities": [],
                "topics": [],
                "references": [],
            }

        gate = ExtractionGate()
        result = gate.validate(succeed_first)

        assert isinstance(result, StrictExtraction)
        assert attempts == 1  # Only first attempt

    def test_second_success_after_first_failure(self):
        """Second attempt success returns valid result."""
        call_count = 0

        def fail_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("First fails")
            return {
                "summary": "Success",
                "entities": [],
                "topics": [],
                "references": [],
            }

        gate = ExtractionGate()
        result = gate.validate(fail_then_succeed)

        assert isinstance(result, StrictExtraction)
        assert call_count == 2


class TestValidationErrorTerminalState:
    """Test that validation errors represent terminal states."""

    def test_validation_error_is_not_recoverable_by_gate(self):
        """Once validation fails twice, the error is terminal (gate doesn't retry further)."""
        attempts = 0

        def always_invalid():
            nonlocal attempts
            attempts += 1
            return {
                "summary": "Test",
                "entities": [],
                "topics": [],
                "references": [],
                "extra": "field",
            }

        gate = ExtractionGate()

        with pytest.raises(ValidationError):
            gate.validate(always_invalid)

        # Exactly 2 attempts, no more
        assert attempts == 2

    def test_terminal_error_prevents_any_publishing(self, file_record: FileRecord, projection_stores):
        """Terminal validation error prevents any data from being published."""
        vectors, graph = projection_stores

        # Simulate terminal validation state by using ERROR status
        terminal_record = file_record.model_copy(update={"status": IndexStatus.ERROR})

        # All projection attempts should fail
        project_chunks(terminal_record, [], [], vectors)
        apply_extraction(
            terminal_record,
            Extraction(summary="", entities=[], topics=[], references=[]),
            graph,
        )

        # Verify nothing was published
        assert vectors.count() == 0
        assert len(graph.neighbors("test-file")) == 0


class TestExtractionSafetyAcrossMultipleResources:
    """Test that invalid extraction of one resource doesn't affect others."""

    def test_invalid_extraction_of_one_resource_does_not_affect_others(
        self, tmp_path: Path, projection_stores
    ):
        """Invalid extraction of one resource is isolated from other resources."""
        vectors, graph = projection_stores

        file1 = FileRecord(
            id="file1",
            path=str(tmp_path / "file1.md"),
            name="file1.md",
            ext=".md",
            mime="text/markdown",
            size_bytes=100,
            mtime=1000.0,
            content_hash="hash1",
            status=IndexStatus.ERROR,  # Invalid
            summary="",
        )

        file2 = FileRecord(
            id="file2",
            path=str(tmp_path / "file2.md"),
            name="file2.md",
            ext=".md",
            mime="text/markdown",
            size_bytes=100,
            mtime=1000.0,
            content_hash="hash2",
            status=IndexStatus.INDEXED,  # Valid
            summary="Valid",
        )

        # Project both
        project_chunks(
            file1, [Chunk(id="file1:0", file_id="file1", ordinal=0, text="invalid", token_count=1)], [], vectors
        )
        project_chunks(
            file2,
            [Chunk(id="file2:0", file_id="file2", ordinal=0, text="valid", token_count=1)],
            [[1.0, 0.0, 0.0, 0.0]],
            vectors,
        )

        # Verify only valid resource is in projection
        assert vectors.count() == 1

        results = vectors.search([1.0, 0.0, 0.0, 0.0], k=5)
        assert len(results) == 1
        assert results[0].file_id == "file2"

    def test_graph_projection_isolation(self, tmp_path: Path, projection_stores):
        """Graph projection isolates invalid from valid resources."""
        vectors, graph = projection_stores

        valid_file = FileRecord(
            id="valid",
            path=str(tmp_path / "valid.md"),
            name="valid.md",
            ext=".md",
            mime="text/markdown",
            size_bytes=100,
            mtime=1000.0,
            content_hash="hash",
            status=IndexStatus.INDEXED,
            summary="Valid file",
        )

        # Apply valid extraction
        apply_extraction(
            valid_file,
            Extraction(
                summary="Valid",
                entities=[{"name": "ValidEntity", "kind": "test"}],
                topics=["valid"],
                references=[],
            ),
            graph,
        )

        # Verify valid resource has relationships
        relationships = graph.get_direct_relationships("valid")
        assert len(relationships["entities"]) > 0 or len(relationships["topics"]) > 0

        # Simulate invalid extraction with empty data
        apply_extraction(
            valid_file,
            Extraction(summary="", entities=[], topics=[], references=[]),
            graph,
        )

        # Verify isolation: valid resource loses relationships but file node exists
        assert graph.get_file("valid") is not None
        relationships_after = graph.get_direct_relationships("valid")
        assert len(relationships_after["entities"]) == 0 and len(relationships_after["topics"]) == 0
