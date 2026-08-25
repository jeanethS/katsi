"""Compatible content-hash cache lookup for media pipeline stages.

Extends the :class:`~katsi_core.media.registry.RepresentationRegistry`
pattern (itself following the path-independent ``EnrichmentCache`` used for
text enrichment, see ``katsi_core.store.enrichment_cache``) with
*compatible* lookup semantics: a successful representation is reusable for
any resource version whose pipeline fingerprint digest matches, regardless
of which concrete ``resource_version_id`` originally produced it.

Because :class:`~katsi_core.media.contracts.PipelineFingerprint` does not
carry a ``resource_version_id`` (only a ``source_content_hash``), two
resource versions with identical bytes and identical policy/model
configuration always produce identical fingerprints. This is what lets
copied media and A -> B -> A file histories reuse a successful
representation instead of repeating expensive work: Katsi never re-runs a
pipeline stage against content it has already processed successfully under
the same policy.

Failed runs are recorded (via ``RepresentationLifecycleManager``) for
diagnostics but never satisfy a compatible lookup: only ``CURRENT`` and
``PARTIAL`` representations are eligible, and ``PARTIAL`` results are never
reported as if they were complete understanding.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from katsi_core.media.contracts import (
    DerivedRepresentation,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    ResourceVersionId,
)
from katsi_core.media.fingerprint import fingerprint_digest
from katsi_core.media.registry import RepresentationRegistry, _utc_now

_ELIGIBLE_STATUSES = (MediaRepresentationStatus.CURRENT, MediaRepresentationStatus.PARTIAL)


@dataclass(frozen=True, slots=True)
class CacheLookupResult:
    """Outcome of a compatible cache lookup."""

    representation: DerivedRepresentation
    """The representation reused to satisfy the lookup."""

    reused_from_resource_version_id: ResourceVersionId
    """The resource version that originally produced this representation."""

    is_exact_resource_match: bool
    """True if the match already belongs to the requested resource version."""


class RepresentationCache:
    """Compatible cache lookup layered over :class:`RepresentationRegistry`.

    Lookup is content/fingerprint driven, not tied to a single resource
    version: a representation produced for one resource version is
    compatible with -- and reusable by -- any other resource version whose
    fingerprint digest matches (same content hash, same policy, same
    producer identity).
    """

    def __init__(self, registry: RepresentationRegistry) -> None:
        self._registry = registry

    def find_compatible(
        self,
        resource_version_id: ResourceVersionId,
        kind: MediaRepresentationKind,
        fingerprint: PipelineFingerprint,
    ) -> CacheLookupResult | None:
        """Find a compatible successful representation for ``fingerprint``.

        First checks for an exact match already bound to
        ``resource_version_id`` (the common case). Falls back to scanning
        other resource versions' representations of the same ``kind`` for a
        fingerprint-digest match -- this is the path that lets copied media
        and A -> B -> A histories reuse work without recomputation.
        """
        exact = self._registry.find_cached_representation(resource_version_id, kind, fingerprint)
        if exact is not None:
            return CacheLookupResult(
                representation=exact,
                reused_from_resource_version_id=resource_version_id,
                is_exact_resource_match=True,
            )

        candidate = self._registry.find_by_fingerprint_digest(
            kind, fingerprint_digest(fingerprint), _ELIGIBLE_STATUSES
        )
        if candidate is None:
            return None
        return CacheLookupResult(
            representation=candidate,
            reused_from_resource_version_id=candidate.resource_version_id,
            is_exact_resource_match=False,
        )

    def reuse_for_resource(
        self,
        resource_version_id: ResourceVersionId,
        result: CacheLookupResult,
    ) -> DerivedRepresentation:
        """Bind a compatible representation to ``resource_version_id``.

        If the match already belongs to this resource version, it is
        returned unchanged. Otherwise a new immutable representation row is
        registered -- same content, provenance, and fingerprint -- so the
        expensive work (model call, OCR pass, transcription, ...) is never
        repeated for content Katsi has already processed successfully.
        """
        if result.is_exact_resource_match:
            return result.representation

        original = result.representation
        now = _utc_now()
        reused = original.model_copy(
            update={
                "id": uuid4(),
                "resource_version_id": resource_version_id,
                "created_at": now,
                "updated_at": now,
            }
        )
        self._registry.register_representation(reused, make_current=True)
        return reused

    def get_or_mark_miss(
        self,
        resource_version_id: ResourceVersionId,
        kind: MediaRepresentationKind,
        fingerprint: PipelineFingerprint,
    ) -> DerivedRepresentation | None:
        """Single-call convenience: return a reused representation, or ``None`` on a cache miss."""
        result = self.find_compatible(resource_version_id, kind, fingerprint)
        if result is None:
            return None
        return self.reuse_for_resource(resource_version_id, result)


__all__ = [
    "CacheLookupResult",
    "RepresentationCache",
]
