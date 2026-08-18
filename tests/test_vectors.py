"""Tests for VectorStore (LanceDB-backed)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from katsi_core.media.contracts import (
    DerivedRepresentation,
    MediaCoverage,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    PipelineStage,
    ProducerProvenance,
    WholeResourceLocator,
)
from katsi_core.models import Chunk
from katsi_core.store.vectors import VectorStore


def _media_text_representation(
    kind: MediaRepresentationKind = MediaRepresentationKind.OCR_TEXT,
) -> DerivedRepresentation:
    resource_version_id = uuid4()
    representation_id = uuid4()
    now = datetime.now(UTC)
    return DerivedRepresentation(
        id=representation_id,
        resource_version_id=resource_version_id,
        kind=kind,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=now,
        updated_at=now,
        textual_payload="OCR text from a screenshot",
        locators=(
            WholeResourceLocator(
                resource_version_id=resource_version_id,
                representation_id=representation_id,
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="fake-ocr",
            adapter_version="1",
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash="a" * 64,
            representation_kind=kind,
            stage=PipelineStage.EXTRACT_TEXT,
            adapter_name="fake-ocr",
            adapter_version="1",
            sampling_fingerprint="b" * 64,
        ),
    )


def test_init_creates_table(tmp_path):
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=8)
    assert vs.count() == 0
    # Second call is a no-op
    vs.init_table(embed_dim=8)
    assert vs.count() == 0


def test_count_opens_existing_table(tmp_path):
    first = VectorStore(tmp_path / "vectors", "test_chunks")
    first.init_table(embed_dim=4)
    first.upsert_chunks(
        [Chunk(id="c1", file_id="f1", ordinal=0, text="hello", token_count=1)],
        [[1.0, 0.0, 0.0, 0.0]],
    )

    reopened = VectorStore(tmp_path / "vectors", "test_chunks")

    assert reopened.count() == 1


@pytest.mark.parametrize(
    "kind",
    [
        MediaRepresentationKind.OCR_TEXT,
        MediaRepresentationKind.IMAGE_CAPTION,
        MediaRepresentationKind.TRANSCRIPT_SEGMENT,
    ],
)
def test_media_text_projection_preserves_representation_and_locator_metadata(tmp_path, kind):
    vector_store = VectorStore(tmp_path / "vectors", "test_chunks")
    representation = _media_text_representation(kind)

    vector_store.upsert_media_text([representation], [[1.0, 0.0, 0.0, 0.0]])

    results = vector_store.search_media_text([1.0, 0.0, 0.0, 0.0])
    assert len(results) == 1
    result = results[0]
    assert result.representation_id == representation.id
    assert result.resource_version_id == representation.resource_version_id
    assert result.kind is kind
    assert result.locators[0]["locator_type"] == "whole_resource"


def test_media_text_projection_removes_stale_resource_version(tmp_path):
    vector_store = VectorStore(tmp_path / "vectors", "test_chunks")
    representation = _media_text_representation()
    vector_store.upsert_media_text([representation], [[1.0, 0.0, 0.0, 0.0]])

    vector_store.delete_media_by_resource_version(representation.resource_version_id)

    assert vector_store.search_media_text([1.0, 0.0, 0.0, 0.0]) == []


def test_upsert_and_search(tmp_path):
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=4)

    chunks = [
        Chunk(id="c1", file_id="f1", ordinal=0, text="hello world", token_count=2),
        Chunk(id="c2", file_id="f1", ordinal=1, text="goodbye world", token_count=2),
    ]
    vectors = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
    vs.upsert_chunks(chunks, vectors)

    # Search with the first vector — c1 should be first
    results = vs.search([1.0, 0.0, 0.0, 0.0], k=2)
    assert len(results) == 2
    assert results[0][0] == "c1"  # chunk_id
    assert results[0][1] == "f1"  # file_id


def test_upsert_replaces_by_file_id(tmp_path):
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=4)

    chunks_a = [
        Chunk(id="c1", file_id="f1", ordinal=0, text="a", token_count=1),
        Chunk(id="c2", file_id="f1", ordinal=1, text="b", token_count=1),
    ]
    vs.upsert_chunks(chunks_a, [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])
    assert vs.count() == 2

    chunks_b = [
        Chunk(id="c3", file_id="f1", ordinal=0, text="c", token_count=1),
    ]
    vs.upsert_chunks(chunks_b, [[0.5, 0.5, 0.0, 0.0]])
    # Old chunks replaced; count should match latest set
    assert vs.count() == 1


def test_delete_by_file(tmp_path):
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=4)

    chunks = [
        Chunk(id="c1", file_id="f1", ordinal=0, text="a", token_count=1),
        Chunk(id="c2", file_id="f1", ordinal=1, text="b", token_count=1),
    ]
    vs.upsert_chunks(chunks, [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]])
    assert vs.count() == 2

    vs.delete_by_file("f1")
    assert vs.count() == 0


def test_search_reflects_replaced_current_chunks(tmp_path):
    """Re-upserting a file's chunks must drop the previous version from search."""
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=4)

    vs.upsert_chunks(
        [Chunk(id="c1", file_id="f1", ordinal=0, text="old", token_count=1)],
        [[1.0, 0.0, 0.0, 0.0]],
    )
    # Replace f1's current chunks with a new resource version.
    vs.upsert_chunks(
        [Chunk(id="c2", file_id="f1", ordinal=0, text="new", token_count=1)],
        [[0.0, 1.0, 0.0, 0.0]],
    )

    # A query matching the replaced chunk cannot surface the old version.
    hits = vs.search([1.0, 0.0, 0.0, 0.0], k=5)
    assert all(cid != "c1" for cid, _fid, _score in hits)
    assert any(cid == "c2" for cid, _fid, _score in hits)


def test_delete_by_file_makes_chunks_unsearchable(tmp_path):
    """Deleted resources cannot remain in current search results."""
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=4)

    vs.upsert_chunks(
        [Chunk(id="c1", file_id="f1", ordinal=0, text="a", token_count=1)],
        [[1.0, 0.0, 0.0, 0.0]],
    )
    vs.delete_by_file("f1")

    hits = vs.search([1.0, 0.0, 0.0, 0.0], k=5)
    assert hits == []
    assert all(fid != "f1" for _cid, fid, _score in hits)


def test_search_returns_three_tuple(tmp_path):
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=4)

    chunks = [
        Chunk(id="c1", file_id="f1", ordinal=0, text="hello", token_count=1),
    ]
    vs.upsert_chunks(chunks, [[1.0, 0.0, 0.0, 0.0]])
    results = vs.search([1.0, 0.0, 0.0, 0.0], k=5)

    assert len(results) == 1
    cid, fid, score = results[0]
    assert isinstance(cid, str)
    assert isinstance(fid, str)
    assert isinstance(score, float)
    assert fid == "f1"


def test_empty_upsert_is_noop(tmp_path):
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=4)
    vs.upsert_chunks([], [])  # should not raise
    assert vs.count() == 0


def test_upsert_mismatched_lengths_raises(tmp_path):
    vs = VectorStore(tmp_path / "vectors", "test_chunks")
    vs.init_table(embed_dim=4)
    import pytest

    with pytest.raises(ValueError, match="len.*!=.*len"):
        vs.upsert_chunks(
            [Chunk(id="c1", file_id="f1", ordinal=0, text="a", token_count=1)],
            [],
        )
