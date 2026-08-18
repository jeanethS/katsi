"""Task 9.2--9.7 tests for isolated modality-aware projections."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

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
