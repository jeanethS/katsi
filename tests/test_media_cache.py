"""Tests for compatible content-hash cache lookup.

Covers task 4.3 (copied media / A -> B -> A histories reuse successful
representations) and part of 4.6 (incompatible fingerprint changes produce
new representation versions, including the specific regression for
``ChunkingThresholds.target_tokens``).
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from katsi_core.config import ChunkingThresholds, MediaSamplingSettings, SQLiteSettings
from katsi_core.media.cache import RepresentationCache
from katsi_core.media.contracts import (
    ContentHash,
    DerivedRepresentation,
    MediaCoverage,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineStage,
    ProducerProvenance,
    RepresentationError,
    ResourceVersionId,
    WholeResourceLocator,
)
from katsi_core.media.fingerprint import build_pipeline_fingerprint, fingerprint_digest
from katsi_core.media.registry import RepresentationRegistry
from katsi_core.store.workspace_sqlite import WorkspaceSQLite


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    settings = SQLiteSettings()
    db = WorkspaceSQLite(db_path, settings)
    yield db
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def registry(temp_db):
    return RepresentationRegistry(temp_db)


@pytest.fixture
def cache(registry):
    return RepresentationCache(registry)


@pytest.fixture
def producer():
    return ProducerProvenance(
        producer_type=MediaProducerType.DETERMINISTIC,
        adapter_name="text_chunker",
        adapter_version="1.0.0",
    )


def _fingerprint(settings=None, **overrides):
    settings = settings or MediaSamplingSettings()
    kwargs = dict(
        source_content_hash=ContentHash("a" * 64),
        representation_kind=MediaRepresentationKind.EXTRACTED_TEXT,
        stage=PipelineStage.EXTRACT_TEXT,
        adapter_name="text_chunker",
        adapter_version="1.0.0",
        settings=settings,
    )
    kwargs.update(overrides)
    return build_pipeline_fingerprint(**kwargs)


def _representation(
    resource_version_id, fingerprint, producer, status=MediaRepresentationStatus.CURRENT
):
    now = datetime.now(UTC)
    return DerivedRepresentation(
        id=uuid4(),
        resource_version_id=resource_version_id,
        kind=MediaRepresentationKind.EXTRACTED_TEXT,
        media_type="text/plain",
        status=status,
        created_at=now,
        updated_at=now,
        textual_payload="expensive extracted text",
        locators=(
            WholeResourceLocator(
                resource_version_id=resource_version_id, representation_id=uuid4()
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=producer,
        pipeline_fingerprint=fingerprint,
    )


# =============================================================================
# 4.3: compatible reuse across copies and A -> B -> A histories
# =============================================================================


def test_exact_resource_match_is_reused_without_scan(cache, registry, producer):
    resource_id = ResourceVersionId(str(uuid4()))
    fingerprint = _fingerprint()
    existing = _representation(resource_id, fingerprint, producer)
    registry.register_representation(existing, make_current=True)

    result = cache.find_compatible(resource_id, MediaRepresentationKind.EXTRACTED_TEXT, fingerprint)

    assert result is not None
    assert result.is_exact_resource_match is True
    assert result.representation.id == existing.id


def test_copied_file_reuses_representation_without_recompute(cache, registry, producer):
    """Two distinct resource versions with identical content hash + policy: the
    second lookup must find the first's successful work and reuse it, never
    recomputing.
    """
    original_resource_id = ResourceVersionId(str(uuid4()))
    copied_resource_id = ResourceVersionId(str(uuid4()))
    fingerprint = _fingerprint()  # same source_content_hash for both "files"

    original = _representation(original_resource_id, fingerprint, producer)
    registry.register_representation(original, make_current=True)

    result = cache.find_compatible(
        copied_resource_id, MediaRepresentationKind.EXTRACTED_TEXT, fingerprint
    )
    assert result is not None
    assert result.is_exact_resource_match is False
    assert result.reused_from_resource_version_id == original_resource_id

    reused = cache.reuse_for_resource(copied_resource_id, result)
    assert reused.resource_version_id == copied_resource_id
    assert reused.textual_payload == original.textual_payload
    assert reused.id != original.id  # new immutable row, not the same representation

    # The copy is now itself directly (exact-match) cached for future lookups.
    direct = cache.find_compatible(
        copied_resource_id, MediaRepresentationKind.EXTRACTED_TEXT, fingerprint
    )
    assert direct is not None
    assert direct.is_exact_resource_match is True


def test_a_to_b_to_a_history_reuses_original_representation(cache, registry, producer):
    """A file edited to B and back to A (same bytes as the original A) should
    reuse A's original representation rather than reprocessing.
    """
    resource_a = ResourceVersionId(str(uuid4()))
    resource_b = ResourceVersionId(str(uuid4()))
    resource_a_again = ResourceVersionId(str(uuid4()))  # new immutable version, same content as A

    fingerprint_a = _fingerprint(source_content_hash=ContentHash("a" * 64))
    fingerprint_b = _fingerprint(source_content_hash=ContentHash("b" * 64))

    rep_a = _representation(resource_a, fingerprint_a, producer)
    registry.register_representation(rep_a, make_current=True)

    rep_b = _representation(resource_b, fingerprint_b, producer)
    registry.register_representation(rep_b, make_current=True)

    # Content reverts to A's bytes -> A's fingerprint again.
    result = cache.find_compatible(
        resource_a_again, MediaRepresentationKind.EXTRACTED_TEXT, fingerprint_a
    )

    assert result is not None
    assert result.reused_from_resource_version_id == resource_a
    assert result.representation.textual_payload == rep_a.textual_payload


def test_get_or_mark_miss_returns_none_when_nothing_compatible(cache):
    resource_id = ResourceVersionId(str(uuid4()))
    fingerprint = _fingerprint()

    result = cache.get_or_mark_miss(
        resource_id, MediaRepresentationKind.EXTRACTED_TEXT, fingerprint
    )

    assert result is None


def test_failed_representation_does_not_satisfy_compatible_lookup(cache, registry, producer):
    """Failed runs are recorded for diagnostics but never satisfy a future compatible lookup."""
    resource_id = ResourceVersionId(str(uuid4()))
    other_resource_id = ResourceVersionId(str(uuid4()))
    fingerprint = _fingerprint()

    failed = DerivedRepresentation(
        id=uuid4(),
        resource_version_id=resource_id,
        kind=MediaRepresentationKind.EXTRACTED_TEXT,
        media_type="text/plain",
        status=MediaRepresentationStatus.FAILED,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        textual_payload="",
        locators=(),
        coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0),
        producer=producer,
        pipeline_fingerprint=fingerprint,
        error=RepresentationError(error_category="io_error", error_message="disk read failed"),
    )
    registry.register_representation(failed, make_current=False)

    result = cache.find_compatible(
        other_resource_id, MediaRepresentationKind.EXTRACTED_TEXT, fingerprint
    )

    assert result is None


def test_partial_representation_is_reused_but_marked_partial(cache, registry, producer):
    resource_id = ResourceVersionId(str(uuid4()))
    other_resource_id = ResourceVersionId(str(uuid4()))
    fingerprint = _fingerprint()

    partial = _representation(
        resource_id, fingerprint, producer, status=MediaRepresentationStatus.PARTIAL
    )
    partial = partial.model_copy(
        update={"coverage": MediaCoverage(is_complete=False, coverage_fraction=0.5)}
    )
    registry.register_representation(partial, make_current=True)

    result = cache.find_compatible(
        other_resource_id, MediaRepresentationKind.EXTRACTED_TEXT, fingerprint
    )

    assert result is not None
    reused = cache.reuse_for_resource(other_resource_id, result)
    assert reused.status == MediaRepresentationStatus.PARTIAL
    assert reused.coverage.is_complete is False


# =============================================================================
# 4.6: incompatible fingerprint changes produce new representation versions
# =============================================================================


def test_changed_target_tokens_produces_cache_miss_not_reuse(cache, registry, producer):
    """The core regression test: changing ChunkingThresholds.target_tokens must
    produce a different fingerprint digest, so the compatible cache lookup
    misses and a NEW representation version is produced rather than silently
    reusing chunks built under the old token target.
    """
    resource_id = ResourceVersionId(str(uuid4()))
    new_resource_id = ResourceVersionId(str(uuid4()))

    default_settings = MediaSamplingSettings()
    changed_settings = MediaSamplingSettings(
        chunking=ChunkingThresholds(target_tokens=1024, overlap=64)
    )

    default_fingerprint = _fingerprint(default_settings)
    changed_fingerprint = _fingerprint(changed_settings)

    # Sanity: this is the exact bug being fixed -- digests must differ.
    assert fingerprint_digest(default_fingerprint) != fingerprint_digest(changed_fingerprint)

    original = _representation(resource_id, default_fingerprint, producer)
    registry.register_representation(original, make_current=True)

    # Looking up with the OLD policy still finds the cached representation.
    old_policy_result = cache.find_compatible(
        new_resource_id, MediaRepresentationKind.EXTRACTED_TEXT, default_fingerprint
    )
    assert old_policy_result is not None

    # Looking up with the NEW (changed target_tokens) policy must miss --
    # the old chunked representation is not a compatible substitute.
    new_policy_result = cache.find_compatible(
        new_resource_id, MediaRepresentationKind.EXTRACTED_TEXT, changed_fingerprint
    )
    assert new_policy_result is None


def test_changed_target_tokens_registers_distinct_representation_version(cache, registry, producer):
    resource_id = ResourceVersionId(str(uuid4()))

    default_settings = MediaSamplingSettings()
    changed_settings = MediaSamplingSettings(
        chunking=ChunkingThresholds(target_tokens=2048, overlap=32)
    )

    default_fingerprint = _fingerprint(default_settings)
    changed_fingerprint = _fingerprint(changed_settings)

    original = _representation(resource_id, default_fingerprint, producer)
    registry.register_representation(original, make_current=True)

    # No compatible cached result under the new policy -> caller must produce
    # a new representation version (simulated here by registering one).
    assert (
        cache.find_compatible(
            resource_id, MediaRepresentationKind.EXTRACTED_TEXT, changed_fingerprint
        )
        is None
    )

    new_version = _representation(resource_id, changed_fingerprint, producer)
    registry.register_representation(new_version, make_current=True)

    # Both versions persist in history; the new one is retrievable by its own fingerprint.
    result = cache.find_compatible(
        resource_id, MediaRepresentationKind.EXTRACTED_TEXT, changed_fingerprint
    )
    assert result is not None
    assert result.representation.id == new_version.id
    assert result.representation.pipeline_fingerprint.sampling_fingerprint != (
        original.pipeline_fingerprint.sampling_fingerprint
    )


def test_changed_adapter_version_also_produces_cache_miss(cache, registry, producer):
    resource_id = ResourceVersionId(str(uuid4()))
    new_resource_id = ResourceVersionId(str(uuid4()))

    fingerprint_v1 = _fingerprint(adapter_version="1.0.0")
    fingerprint_v2 = _fingerprint(adapter_version="2.0.0")

    original = _representation(resource_id, fingerprint_v1, producer)
    registry.register_representation(original, make_current=True)

    result = cache.find_compatible(
        new_resource_id, MediaRepresentationKind.EXTRACTED_TEXT, fingerprint_v2
    )
    assert result is None


def test_legacy_database_without_digest_column_is_migrated_and_still_reuses(
    temp_db, registry, producer
):
    """A database created before ``fingerprint_digest`` keeps reusing its rows."""
    resource_id = ResourceVersionId(str(uuid4()))
    other_resource_id = ResourceVersionId(str(uuid4()))
    fingerprint = _fingerprint()
    registry.register_representation(
        _representation(resource_id, fingerprint, producer), make_current=True
    )

    # Simulate the pre-migration schema: drop the digest column entirely.
    with temp_db.connection() as conn:
        conn.execute("DROP INDEX idx_representations_fingerprint_digest")
        conn.execute("ALTER TABLE representations DROP COLUMN fingerprint_digest")

    migrated = RepresentationCache(RepresentationRegistry(temp_db))
    result = migrated.find_compatible(
        other_resource_id, MediaRepresentationKind.EXTRACTED_TEXT, fingerprint
    )

    assert result is not None
    assert result.is_exact_resource_match is False
    assert result.representation.resource_version_id == resource_id
