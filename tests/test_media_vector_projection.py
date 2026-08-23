"""Task 9.2--9.7 tests for isolated modality-aware projections."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from katsi_core.media.contracts import (
    DerivedRepresentation,
    ImageRegionLocator,
    MediaCoverage,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    PipelineStage,
    ProducerProvenance,
    TimeRangeLocator,
    WholeResourceLocator,
)
from katsi_core.retrieve.media import (
    MediaQueryRoute,
    available_routes,
    fuse_media_results,
    search_media,
)
from katsi_core.store.graph import GraphStore
from katsi_core.store.vectors import VectorStore


def _representation(*, space: str = "clip_vit_b_32", vector: list[float] | None = None):
    resource_id, representation_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    return DerivedRepresentation(
        id=representation_id,
        resource_version_id=resource_id,
        kind=MediaRepresentationKind.VISUAL_EMBEDDING,
        media_type="image/png",
        status=MediaRepresentationStatus.CURRENT,
        created_at=now,
        updated_at=now,
        textual_payload=json.dumps({"space": space, "embedding": vector or [1.0, 0.0]}),
        locators=(
            WholeResourceLocator(
                resource_version_id=resource_id, representation_id=representation_id
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.MODEL_BACKED, adapter_name="fake", adapter_version="1"
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash="a" * 64,
            representation_kind=MediaRepresentationKind.VISUAL_EMBEDDING,
            stage=PipelineStage.EMBED_VISUAL,
            adapter_name="fake",
            adapter_version="1",
            sampling_fingerprint="b" * 64,
        ),
    )


def test_visual_indexes_are_isolated_by_embedding_space_and_dimension(tmp_path):
    vectors = VectorStore(tmp_path / "vectors")
    clip = _representation(space="clip", vector=[1.0, 0.0])
    siglip = _representation(space="siglip", vector=[1.0, 0.0, 0.0])
    vectors.upsert_visual_embeddings([clip, siglip])

    assert [hit.representation_id for hit in vectors.search_visual("clip", [1.0, 0.0])] == [clip.id]
    assert vectors.search_visual("clip", [1.0, 0.0, 0.0]) == []
    assert [hit.representation_id for hit in vectors.search_visual("siglip", [1.0, 0.0, 0.0])] == [
        siglip.id
    ]


def test_projection_rebuild_reuses_cached_visual_representations(tmp_path):
    vectors = VectorStore(tmp_path / "vectors")
    representation = _representation()
    vectors.upsert_visual_embeddings([representation])
    vectors.rebuild_media_projections([representation])

    assert (
        vectors.search_visual("clip_vit_b_32", [1.0, 0.0])[0].representation_id == representation.id
    )


class _Encoder:
    space = "clip"
    supports_text = True
    supports_image = True

    def embed_text(self, query: str) -> list[float]:
        return [1.0, 0.0]

    def embed_image(self, image: bytes) -> list[float]:
        return [1.0, 0.0]


def test_query_routes_and_fusion_are_capability_and_space_checked(tmp_path):
    vectors = VectorStore(tmp_path / "vectors")
    representation = _representation(space="clip")
    vectors.upsert_visual_embeddings([representation])
    encoder = _Encoder()

    assert available_routes(cross_modal_enabled=False, encoder=encoder, image_authorized=False) == {
        MediaQueryRoute.TEXT_TO_TEXT
    }
    routed = search_media(
        vectors,
        "a photo",
        encoder=encoder,
        cross_modal_enabled=True,
        image_query=b"image",
        image_authorized=True,
    )
    fused = fuse_media_results(routed)
    assert {MediaQueryRoute.TEXT_TO_VISUAL, MediaQueryRoute.IMAGE_TO_VISUAL} == set(routed)
    assert fused[0].resource_version_id == representation.resource_version_id
    assert all(signal.calibrated_score == 1.0 for signal in fused[0].contributions)


def test_graph_projection_removes_noncurrent_visibility(tmp_path):
    graph = GraphStore(tmp_path / "graph")
    representation = _representation()
    graph.project_media_representations([representation])
    assert graph._conn.execute("MATCH (p:MediaRepresentation) RETURN count(p)").get_next()[0] == 1

    graph.remove_media_resource_projection(representation.resource_version_id)
    assert graph._conn.execute("MATCH (p:MediaRepresentation) RETURN count(p)").get_next()[0] == 0


def _time_ranged_representation(
    *,
    kind: MediaRepresentationKind,
    stage: PipelineStage,
    adapter_name: str,
    start_ms: int,
    end_ms: int,
    media_type: str,
    textual_payload: str,
):
    """A representation carrying exactly one TimeRangeLocator.

    Both silence spans and transcript segments are time-ranged, which is
    precisely why graph projection must dispatch on kind rather than on
    locator_type.
    """
    resource_id, representation_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    return DerivedRepresentation(
        id=representation_id,
        resource_version_id=resource_id,
        kind=kind,
        media_type=media_type,
        status=MediaRepresentationStatus.CURRENT,
        created_at=now,
        updated_at=now,
        textual_payload=textual_payload,
        locators=(
            TimeRangeLocator(
                resource_version_id=resource_id,
                representation_id=representation_id,
                start_ms=start_ms,
                end_ms=end_ms,
            ),
        ),
        coverage=MediaCoverage(is_complete=False, coverage_fraction=0.5),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name=adapter_name,
            adapter_version="1",
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash="a" * 64,
            representation_kind=kind,
            stage=stage,
            adapter_name=adapter_name,
            adapter_version="1",
            sampling_fingerprint="b" * 64,
        ),
    )


def _silence_representation(*, start_ms: int = 1000, end_ms: int = 2000):
    return _time_ranged_representation(
        kind=MediaRepresentationKind.SILENCE_SPAN,
        stage=PipelineStage.DETECT_SILENCE,
        adapter_name="audio_silence_detect_ffmpeg",
        start_ms=start_ms,
        end_ms=end_ms,
        media_type="application/json",
        textual_payload="",
    )


def _transcript_representation(*, start_ms: int = 0, end_ms: int = 500):
    return _time_ranged_representation(
        kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
        stage=PipelineStage.TRANSCRIBE,
        adapter_name="audio_transcribe_whisper",
        start_ms=start_ms,
        end_ms=end_ms,
        media_type="text/plain",
        textual_payload="hello world",
    )


def test_silence_span_projects_with_its_times(tmp_path):
    graph = GraphStore(tmp_path / "graph")
    representation = _silence_representation(start_ms=1000, end_ms=2000)

    graph.project_media_representations([representation])

    row = graph._conn.execute(
        "MATCH (r:MediaResourceVersion {id: $id})-[:HAS_SILENCE_SPAN]->(s:SilenceSpan) "
        "RETURN s.start_ms, s.end_ms",
        {"id": str(representation.resource_version_id)},
    ).get_next()
    assert row == [1000, 2000]


def test_transcript_segment_still_projects_alongside_silence(tmp_path):
    """Regression guard: silence dispatch must not steal time_range locators."""
    graph = GraphStore(tmp_path / "graph")
    transcript = _transcript_representation()

    graph.project_media_representations([transcript, _silence_representation()])

    count = graph._conn.execute(
        "MATCH (r:MediaResourceVersion {id: $id})-[:HAS_TRANSCRIPT_SEGMENT]->(t:TranscriptSegment) "
        "RETURN count(t)",
        {"id": str(transcript.resource_version_id)},
    ).get_next()[0]
    assert count == 1


def test_silence_span_does_not_create_a_transcript_edge(tmp_path):
    graph = GraphStore(tmp_path / "graph")
    representation = _silence_representation()

    graph.project_media_representations([representation])

    count = graph._conn.execute(
        "MATCH (r:MediaResourceVersion {id: $id})-[:HAS_TRANSCRIPT_SEGMENT]->(t:TranscriptSegment) "
        "RETURN count(t)",
        {"id": str(representation.resource_version_id)},
    ).get_next()[0]
    assert count == 0


def _visual_region_representation(*, label: str = "train", bbox=(0.1, 0.2, 0.4, 0.5)):
    resource_id, representation_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    return DerivedRepresentation(
        id=representation_id,
        resource_version_id=resource_id,
        kind=MediaRepresentationKind.VISUAL_REGION,
        media_type="application/json",
        status=MediaRepresentationStatus.CURRENT,
        created_at=now,
        updated_at=now,
        textual_payload=label,
        locators=(
            ImageRegionLocator(
                resource_version_id=resource_id,
                representation_id=representation_id,
                bounding_box=bbox,
            ),
        ),
        coverage=MediaCoverage(is_complete=False, coverage_fraction=0.2),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.MODEL_BACKED,
            adapter_name="image_detect_regions",
            adapter_version="1",
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash="a" * 64,
            representation_kind=MediaRepresentationKind.VISUAL_REGION,
            stage=PipelineStage.DETECT_REGIONS,
            adapter_name="image_detect_regions",
            adapter_version="1",
            sampling_fingerprint="b" * 64,
        ),
    )


def _ocr_representation():
    """OCR also carries image_region locators -- the trap this guards."""
    resource_id, representation_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    return DerivedRepresentation(
        id=representation_id,
        resource_version_id=resource_id,
        kind=MediaRepresentationKind.OCR_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=now,
        updated_at=now,
        textual_payload="PLATFORM 3",
        locators=(
            ImageRegionLocator(
                resource_version_id=resource_id,
                representation_id=representation_id,
                bounding_box=(0.5, 0.5, 0.2, 0.1),
            ),
        ),
        coverage=MediaCoverage(is_complete=False, coverage_fraction=0.1),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="image_ocr",
            adapter_version="1",
        ),
        pipeline_fingerprint=PipelineFingerprint(
            source_content_hash="c" * 64,
            representation_kind=MediaRepresentationKind.OCR_TEXT,
            stage=PipelineStage.OCR,
            adapter_name="image_ocr",
            adapter_version="1",
            sampling_fingerprint="d" * 64,
        ),
    )


def test_visual_region_projects_with_label_and_box(tmp_path):
    graph = GraphStore(tmp_path / "graph")
    representation = _visual_region_representation(label="train", bbox=(0.1, 0.2, 0.4, 0.5))

    graph.project_media_representations([representation])

    row = graph._conn.execute(
        "MATCH (r:MediaResourceVersion {id: $id})-[:HAS_VISUAL_REGION]->(v:VisualRegion) "
        "RETURN v.label, v.x, v.y, v.width, v.height",
        {"id": str(representation.resource_version_id)},
    ).get_next()
    assert row[0] == "train"
    assert row[1:] == pytest.approx([0.1, 0.2, 0.4, 0.5])


def test_ocr_is_not_captured_as_a_visual_region(tmp_path):
    """Regression guard: OCR also carries image_region locators."""
    graph = GraphStore(tmp_path / "graph")
    ocr = _ocr_representation()

    graph.project_media_representations([ocr, _visual_region_representation()])

    count = graph._conn.execute(
        "MATCH (r:MediaResourceVersion {id: $id})-[:HAS_VISUAL_REGION]->(v:VisualRegion) "
        "RETURN count(v)",
        {"id": str(ocr.resource_version_id)},
    ).get_next()[0]
    assert count == 0
