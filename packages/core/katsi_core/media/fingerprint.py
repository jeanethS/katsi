"""Pipeline fingerprint computation for content-hash caching.

A pipeline fingerprint captures every input that can affect a derived
representation's output: the source content hash, the input representation
used by downstream stages, the representation kind and pipeline stage,
adapter/contract identity, model identity, prompt version, language policy,
and the sampling/chunking policy in effect.

Per design Decision 16 ("Chunking policy changes produce new representation
versions"), the sampling/chunking policy fingerprint MUST be derived from
``MediaSamplingSettings.get_fingerprint_components()`` so that any change to
configured thresholds (target token count, overlap, separator hierarchy, ...)
produces a different fingerprint digest and therefore a new representation
version rather than silently reusing chunks produced under a different
policy.

Fingerprint digests are computed with blake3 over a deterministic
(sorted-key) JSON serialization so that equal fingerprint components always
hash identically regardless of dict insertion order.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import blake3

from katsi_core.config import MediaSamplingSettings
from katsi_core.media.contracts import (
    ContentHash,
    MediaRepresentationKind,
    PipelineFingerprint,
    PipelineStage,
)


def _stable_digest(components: dict[str, Any]) -> str:
    """Blake3 hash of a deterministic (sorted-key) JSON encoding.

    Tuples/lists are normalized to lists by ``json.dumps`` so that the same
    logical sequence always serializes identically.
    """
    payload = json.dumps(components, sort_keys=True, separators=(",", ":"), default=str)
    return blake3.blake3(payload.encode("utf-8")).hexdigest()


def compute_sampling_fingerprint(settings: MediaSamplingSettings) -> str:
    """Compute the sampling/chunking policy fingerprint component.

    This MUST be included in every :class:`PipelineFingerprint` so that
    chunking policy changes (e.g. ``ChunkingThresholds.target_tokens``)
    invalidate cached representations rather than silently reinterpreting
    old chunks under a new policy.
    """
    return _stable_digest(dict(settings.get_fingerprint_components()))


def build_pipeline_fingerprint(
    *,
    source_content_hash: ContentHash,
    representation_kind: MediaRepresentationKind,
    stage: PipelineStage,
    adapter_name: str,
    adapter_version: str,
    settings: MediaSamplingSettings,
    input_representation_id: UUID | None = None,
    model_identity: str | None = None,
    model_version: str | None = None,
    executable_policy: str | None = None,
    language_policy: str = "*",
    ocr_language: str | None = None,
    prompt_version: str | None = None,
    normalization_version: str = "v1",
) -> PipelineFingerprint:
    """Build a complete :class:`PipelineFingerprint`.

    Every input that can change a stage's output is bound into the
    fingerprint: source hash, input representation, adapter/contract
    version, model/tool identity, prompt version, language policy, the
    owner-configured executable policy, and the sampling/chunking policy
    fingerprint derived from ``settings``.
    """
    return PipelineFingerprint(
        source_content_hash=source_content_hash,
        input_representation_id=input_representation_id,
        representation_kind=representation_kind,
        stage=stage,
        adapter_name=adapter_name,
        adapter_version=adapter_version,
        model_identity=model_identity,
        model_version=model_version,
        sampling_fingerprint=compute_sampling_fingerprint(settings),
        executable_policy=executable_policy,
        language_policy=language_policy,
        ocr_language=ocr_language,
        prompt_version=prompt_version,
        normalization_version=normalization_version,
    )


def fingerprint_digest(fingerprint: PipelineFingerprint) -> str:
    """Compute the deterministic blake3 digest used as the cache key.

    Two fingerprints with identical cache-key components (independent of the
    concrete resource_version they were computed for, since
    ``PipelineFingerprint`` does not carry a resource_version_id) always hash
    identically. This is what allows compatible reuse across copied media
    and A -> B -> A file histories: same content hash + same policy => same
    digest => cache hit.
    """
    return _stable_digest(dict(fingerprint.get_cache_key_components()))
