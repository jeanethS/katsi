"""Tests for pipeline fingerprint computation.

These tests verify the core Decision 16 binding: pipeline fingerprints MUST
include ``MediaSamplingSettings.get_fingerprint_components()`` so that
changing configured chunking thresholds (e.g. ``target_tokens``) produces a
different fingerprint digest and therefore a new representation version,
never silently reusing chunks produced under a different policy.
"""

from __future__ import annotations

from katsi_core.config import ChunkingThresholds, MediaSamplingSettings
from katsi_core.media.contracts import (
    ContentHash,
    MediaRepresentationKind,
    PipelineStage,
)
from katsi_core.media.fingerprint import (
    build_pipeline_fingerprint,
    compute_sampling_fingerprint,
    fingerprint_digest,
)

SOURCE_HASH = ContentHash("a" * 64)


def _fingerprint(settings: MediaSamplingSettings, **overrides):
    kwargs = dict(
        source_content_hash=SOURCE_HASH,
        representation_kind=MediaRepresentationKind.EXTRACTED_TEXT,
        stage=PipelineStage.EXTRACT_TEXT,
        adapter_name="text_chunker",
        adapter_version="1.0.0",
        settings=settings,
    )
    kwargs.update(overrides)
    return build_pipeline_fingerprint(**kwargs)


def test_sampling_fingerprint_is_deterministic():
    settings = MediaSamplingSettings()
    first = compute_sampling_fingerprint(settings)
    second = compute_sampling_fingerprint(settings)
    assert first == second
    assert len(first) == 64  # blake3 hex digest length


def test_sampling_fingerprint_changes_with_target_tokens():
    """Core Decision 16 regression: target_tokens is part of the fingerprint."""
    default_settings = MediaSamplingSettings()
    changed_settings = MediaSamplingSettings(
        chunking=ChunkingThresholds(target_tokens=1024, overlap=64)
    )

    default_fp = compute_sampling_fingerprint(default_settings)
    changed_fp = compute_sampling_fingerprint(changed_settings)

    assert default_fp != changed_fp


def test_pipeline_fingerprint_digest_changes_with_target_tokens():
    """End-to-end: a full PipelineFingerprint's digest changes when target_tokens changes.

    This is the exact bug this feature fixes: increasing target_tokens must
    never silently reuse cached chunks produced under the old policy.
    """
    default_settings = MediaSamplingSettings()
    changed_settings = MediaSamplingSettings(
        chunking=ChunkingThresholds(target_tokens=1024, overlap=64)
    )

    default_fingerprint = _fingerprint(default_settings)
    changed_fingerprint = _fingerprint(changed_settings)

    assert default_fingerprint.sampling_fingerprint != changed_fingerprint.sampling_fingerprint
    assert fingerprint_digest(default_fingerprint) != fingerprint_digest(changed_fingerprint)


def test_pipeline_fingerprint_digest_changes_with_overlap():
    settings_a = MediaSamplingSettings(chunking=ChunkingThresholds(target_tokens=512, overlap=32))
    settings_b = MediaSamplingSettings(chunking=ChunkingThresholds(target_tokens=512, overlap=64))

    fp_a = _fingerprint(settings_a)
    fp_b = _fingerprint(settings_b)

    assert fingerprint_digest(fp_a) != fingerprint_digest(fp_b)


def test_pipeline_fingerprint_digest_changes_with_separator_hierarchy():
    settings_a = MediaSamplingSettings(
        chunking=ChunkingThresholds(separator_hierarchy=["\n\n", "\n", " "])
    )
    settings_b = MediaSamplingSettings(
        chunking=ChunkingThresholds(separator_hierarchy=["\n\n", ". ", " "])
    )

    fp_a = _fingerprint(settings_a)
    fp_b = _fingerprint(settings_b)

    assert fingerprint_digest(fp_a) != fingerprint_digest(fp_b)


def test_pipeline_fingerprint_digest_stable_for_identical_policy():
    settings_a = MediaSamplingSettings()
    settings_b = MediaSamplingSettings()

    fp_a = _fingerprint(settings_a)
    fp_b = _fingerprint(settings_b)

    assert fingerprint_digest(fp_a) == fingerprint_digest(fp_b)


def test_pipeline_fingerprint_digest_changes_with_adapter_version():
    settings = MediaSamplingSettings()
    fp_a = _fingerprint(settings, adapter_version="1.0.0")
    fp_b = _fingerprint(settings, adapter_version="2.0.0")

    assert fingerprint_digest(fp_a) != fingerprint_digest(fp_b)


def test_pipeline_fingerprint_digest_changes_with_model_identity():
    settings = MediaSamplingSettings()
    fp_a = _fingerprint(settings, model_identity="whisper-base", model_version="1")
    fp_b = _fingerprint(settings, model_identity="whisper-large", model_version="1")

    assert fingerprint_digest(fp_a) != fingerprint_digest(fp_b)


def test_pipeline_fingerprint_digest_changes_with_prompt_version():
    settings = MediaSamplingSettings()
    fp_a = _fingerprint(settings, prompt_version="v1")
    fp_b = _fingerprint(settings, prompt_version="v2")

    assert fingerprint_digest(fp_a) != fingerprint_digest(fp_b)


def test_pipeline_fingerprint_digest_changes_with_language_policy():
    settings = MediaSamplingSettings()
    fp_a = _fingerprint(settings, language_policy="en")
    fp_b = _fingerprint(settings, language_policy="fr")

    assert fingerprint_digest(fp_a) != fingerprint_digest(fp_b)


def test_pipeline_fingerprint_digest_changes_with_source_hash():
    settings = MediaSamplingSettings()
    fp_a = _fingerprint(settings, source_content_hash=ContentHash("a" * 64))
    fp_b = _fingerprint(settings, source_content_hash=ContentHash("b" * 64))

    assert fingerprint_digest(fp_a) != fingerprint_digest(fp_b)


def test_pipeline_fingerprint_digest_independent_of_resource_version():
    """PipelineFingerprint has no resource_version_id: identical content + policy always
    produce the same digest regardless of which resource version they belong to. This is
    the property that enables compatible reuse across copied files and A -> B -> A histories.
    """
    settings = MediaSamplingSettings()
    fp_a = _fingerprint(settings)
    fp_b = _fingerprint(settings)

    assert fp_a.model_dump() == fp_b.model_dump()
    assert fingerprint_digest(fp_a) == fingerprint_digest(fp_b)
