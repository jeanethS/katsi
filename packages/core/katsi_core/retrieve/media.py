"""Modality-aware media retrieval routes and within-space score fusion."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from katsi_core.media.contracts import DerivedRepresentation, MediaRepresentationStatus
from katsi_core.media.fingerprint import fingerprint_digest
from katsi_core.store.vectors import MediaTextSearchResult, VectorStore, VisualSearchResult

_PREVIEW_CHARS = 480


class MediaQueryRoute(StrEnum):
    TEXT_TO_TEXT = "text_to_text"
    TEXT_TO_VISUAL = "text_to_visual"
    IMAGE_TO_VISUAL = "image_to_visual"


class VisualQueryEncoder(Protocol):
    """Configured local encoder; a space is usable only when it supports the input."""

    space: str
    supports_text: bool
    supports_image: bool

    def embed_text(self, query: str) -> list[float]: ...

    def embed_image(self, image: bytes) -> list[float]: ...


@dataclass(frozen=True)
class MediaSignal:
    resource_version_id: UUID
    representation_id: UUID
    modality: str
    locator_count: int
    raw_score: float
    calibrated_score: float


@dataclass(frozen=True)
class FusedMediaResult:
    resource_version_id: UUID
    score: float
    contributions: tuple[MediaSignal, ...]


@dataclass(frozen=True)
class MediaSearchHit:
    """A bounded, citation-ready media result.

    This intentionally carries references rather than media bytes.  The
    representation remains the authority for provenance and lifecycle state.
    """

    resource_version_id: UUID
    representation_id: UUID
    representation_kind: str
    representation_status: MediaRepresentationStatus
    locators: tuple[dict[str, object], ...]
    coverage_fraction: float
    provenance: dict[str, str]
    relevance_evidence: tuple[MediaSignal, ...]
    score: float
    preview: str | None
    thumbnail_reference: str | None


def _bounded_preview(representation: DerivedRepresentation, max_chars: int) -> str | None:
    """Return a compact text preview; never return binary or a full transcript."""
    if representation.textual_payload is None:
        return None
    compact = " ".join(representation.textual_payload.split())
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 1].rstrip()}…"


def media_search_hits(
    fused: list[FusedMediaResult],
    representations: dict[UUID, DerivedRepresentation],
    *,
    k: int = 8,
    per_resource: int = 1,
    preview_chars: int = _PREVIEW_CHARS,
) -> list[MediaSearchHit]:
    """Materialize fused signals into diverse, bounded source-resource hits.

    A resource can yield many transcript segments or keyframes.  Keeping only
    ``per_resource`` results prevents it from consuming the complete result
    budget while retaining every contributing retrieval signal on the hit.
    """
    if k < 1 or per_resource < 1 or preview_chars < 1:
        raise ValueError("result and preview limits must be positive")
    output: list[MediaSearchHit] = []
    per_resource_counts: dict[UUID, int] = {}
    for result in fused:
        if len(output) >= k:
            break
        for signal in sorted(result.contributions, key=lambda item: -item.calibrated_score):
            if (
                len(output) >= k
                or per_resource_counts.get(result.resource_version_id, 0) >= per_resource
            ):
                break
            representation = representations.get(signal.representation_id)
            if representation is None:
                continue
            if representation.status not in {
                MediaRepresentationStatus.CURRENT,
                MediaRepresentationStatus.PARTIAL,
            }:
                continue
            output.append(
                MediaSearchHit(
                    resource_version_id=result.resource_version_id,
                    representation_id=representation.id,
                    representation_kind=representation.kind.value,
                    representation_status=representation.status,
                    locators=tuple(
                        locator.model_dump(mode="json") for locator in representation.locators
                    ),
                    coverage_fraction=representation.coverage.coverage_fraction,
                    provenance={
                        "producer_type": representation.producer.producer_type.value,
                        "adapter_name": representation.producer.adapter_name,
                        "adapter_version": representation.producer.adapter_version,
                        "pipeline_fingerprint": fingerprint_digest(
                            representation.pipeline_fingerprint
                        ),
                    },
                    relevance_evidence=tuple(
                        item
                        for item in result.contributions
                        if item.representation_id == representation.id
                    ),
                    score=result.score,
                    preview=_bounded_preview(representation, preview_chars),
                    thumbnail_reference=representation.blob_reference
                    if representation.kind.value == "thumbnail"
                    else None,
                )
            )
            per_resource_counts[result.resource_version_id] = (
                per_resource_counts.get(result.resource_version_id, 0) + 1
            )
    return output


def available_routes(
    *, cross_modal_enabled: bool, encoder: VisualQueryEncoder | None, image_authorized: bool
) -> set[MediaQueryRoute]:
    """Return only routes supported by explicit configuration and capability."""
    routes = {MediaQueryRoute.TEXT_TO_TEXT}
    if cross_modal_enabled and encoder is not None and encoder.supports_text:
        routes.add(MediaQueryRoute.TEXT_TO_VISUAL)
    if image_authorized and encoder is not None and encoder.supports_image:
        routes.add(MediaQueryRoute.IMAGE_TO_VISUAL)
    return routes


def search_media(
    vectors: VectorStore,
    query: str | None = None,
    *,
    text_vector: list[float] | None = None,
    encoder: VisualQueryEncoder | None = None,
    cross_modal_enabled: bool = False,
    image_query: bytes | None = None,
    image_authorized: bool = False,
    k: int = 8,
) -> dict[MediaQueryRoute, list[MediaTextSearchResult | VisualSearchResult]]:
    """Route queries without ever searching or fusing incompatible spaces."""
    routes = available_routes(
        cross_modal_enabled=cross_modal_enabled, encoder=encoder, image_authorized=image_authorized
    )
    results: dict[MediaQueryRoute, list[MediaTextSearchResult | VisualSearchResult]] = {}
    if MediaQueryRoute.TEXT_TO_TEXT in routes and text_vector is not None:
        results[MediaQueryRoute.TEXT_TO_TEXT] = vectors.search_media_text(text_vector, k=k)
    if MediaQueryRoute.TEXT_TO_VISUAL in routes and query is not None and encoder is not None:
        visual_query = encoder.embed_text(query)
        results[MediaQueryRoute.TEXT_TO_VISUAL] = vectors.search_visual(
            encoder.space, visual_query, k=k
        )
    if (
        MediaQueryRoute.IMAGE_TO_VISUAL in routes
        and image_query is not None
        and encoder is not None
    ):
        visual_query = encoder.embed_image(image_query)
        results[MediaQueryRoute.IMAGE_TO_VISUAL] = vectors.search_visual(
            encoder.space, visual_query, k=k
        )
    return results


def calibrate_scores(scores: list[float]) -> list[float]:
    """Min-max normalize scores from one space; singleton scores remain intact."""
    if not scores:
        return []
    low, high = min(scores), max(scores)
    if high == low:
        return [1.0] * len(scores)
    return [(score - low) / (high - low) for score in scores]


def fuse_media_results(
    routed: dict[MediaQueryRoute, list[MediaTextSearchResult | VisualSearchResult]],
) -> list[FusedMediaResult]:
    """Calibrate each route independently, then retain all evidence contributions."""
    grouped: dict[UUID, list[MediaSignal]] = {}
    for route, hits in routed.items():
        for hit, calibrated in zip(
            hits, calibrate_scores([item.score for item in hits]), strict=True
        ):
            modality = "text" if isinstance(hit, MediaTextSearchResult) else "visual"
            grouped.setdefault(hit.resource_version_id, []).append(
                MediaSignal(
                    resource_version_id=hit.resource_version_id,
                    representation_id=hit.representation_id,
                    modality=f"{route.value}:{modality}",
                    locator_count=len(hit.locators),
                    raw_score=hit.score,
                    calibrated_score=calibrated,
                )
            )
    return sorted(
        (
            FusedMediaResult(
                resource_version_id=resource_id,
                score=sum(item.calibrated_score for item in signals),
                contributions=tuple(signals),
            )
            for resource_id, signals in grouped.items()
        ),
        key=lambda item: (-item.score, str(item.resource_version_id)),
    )
