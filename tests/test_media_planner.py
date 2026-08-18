"""Tests for the representation DAG planner and stage execution orchestration."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from katsi_core.config import MediaSamplingSettings, SQLiteSettings
from katsi_core.media.cache import RepresentationCache
from katsi_core.media.contracts import (
    ContentHash,
    DerivedRepresentation,
    MediaCoverage,
    MediaPipelineDefinition,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineStage,
    ProducerProvenance,
    ResourceVersionId,
    WholeResourceLocator,
)
from katsi_core.media.fingerprint import build_pipeline_fingerprint
from katsi_core.media.planner import (
    CancellationToken,
    ConcurrencyLimiter,
    ConcurrencyLimitError,
    ConcurrencyLimits,
    DAGCycleError,
    JobResourceClass,
    PipelineDAGPlanner,
    PipelineNodeSpec,
    StageOutcomeStatus,
    StageRunner,
    aggregate_coverage,
    node_spec_from_pipeline_definition,
)
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
def resource_id():
    return ResourceVersionId(str(uuid4()))


def _representation(
    resource_version_id,
    kind,
    fingerprint,
    status=MediaRepresentationStatus.CURRENT,
    coverage=None,
    error=None,
):
    now = datetime.now(UTC)
    return DerivedRepresentation(
        id=uuid4(),
        resource_version_id=resource_version_id,
        kind=kind,
        media_type="text/plain",
        status=status,
        created_at=now,
        updated_at=now,
        textual_payload="content" if status != MediaRepresentationStatus.FAILED else None,
        locators=(
            WholeResourceLocator(
                resource_version_id=resource_version_id, representation_id=uuid4()
            ),
        )
        if status != MediaRepresentationStatus.FAILED
        else (),
        coverage=coverage or MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="test_adapter",
            adapter_version="1.0.0",
        ),
        pipeline_fingerprint=fingerprint,
        error=error,
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


# =============================================================================
# DAG planning (4.1)
# =============================================================================


def test_planner_orders_independent_root_stages_in_one_wave():
    planner = PipelineDAGPlanner(
        [
            PipelineNodeSpec(
                stage=PipelineStage.EXTRACT_METADATA, output_kind=MediaRepresentationKind.METADATA
            ),
            PipelineNodeSpec(
                stage=PipelineStage.EXTRACT_TEXT, output_kind=MediaRepresentationKind.EXTRACTED_TEXT
            ),
        ]
    )
    waves = planner.plan()
    assert len(waves) == 1
    assert {n.stage for n in waves[0]} == {
        PipelineStage.EXTRACT_METADATA,
        PipelineStage.EXTRACT_TEXT,
    }


def test_planner_orders_dependent_stages_into_separate_waves():
    planner = PipelineDAGPlanner(
        [
            PipelineNodeSpec(
                stage=PipelineStage.EXTRACT_TEXT, output_kind=MediaRepresentationKind.EXTRACTED_TEXT
            ),
            PipelineNodeSpec(
                stage=PipelineStage.EMBED_TEXT,
                output_kind=MediaRepresentationKind.TEXT_EMBEDDING,
                input_kinds=frozenset({MediaRepresentationKind.EXTRACTED_TEXT}),
            ),
        ]
    )
    waves = planner.plan()
    assert len(waves) == 2
    assert waves[0][0].stage == PipelineStage.EXTRACT_TEXT
    assert waves[1][0].stage == PipelineStage.EMBED_TEXT


def test_planner_branches_are_independent_of_each_other():
    """OCR and caption both depend on metadata but not on each other -- same wave."""
    planner = PipelineDAGPlanner(
        [
            PipelineNodeSpec(
                stage=PipelineStage.EXTRACT_METADATA, output_kind=MediaRepresentationKind.METADATA
            ),
            PipelineNodeSpec(
                stage=PipelineStage.OCR,
                output_kind=MediaRepresentationKind.OCR_TEXT,
                input_kinds=frozenset({MediaRepresentationKind.METADATA}),
            ),
            PipelineNodeSpec(
                stage=PipelineStage.CAPTION,
                output_kind=MediaRepresentationKind.IMAGE_CAPTION,
                input_kinds=frozenset({MediaRepresentationKind.METADATA}),
            ),
        ]
    )
    waves = planner.plan()
    assert len(waves) == 2
    assert {n.stage for n in waves[1]} == {PipelineStage.OCR, PipelineStage.CAPTION}


def test_planner_raises_on_unsatisfiable_dependency():
    planner = PipelineDAGPlanner(
        [
            PipelineNodeSpec(
                stage=PipelineStage.EMBED_TEXT,
                output_kind=MediaRepresentationKind.TEXT_EMBEDDING,
                input_kinds=frozenset({MediaRepresentationKind.EXTRACTED_TEXT}),
            ),
        ]
    )
    with pytest.raises(DAGCycleError):
        planner.plan()


def test_planner_root_stage_can_use_preexisting_available_kind():
    planner = PipelineDAGPlanner(
        [
            PipelineNodeSpec(
                stage=PipelineStage.EMBED_TEXT,
                output_kind=MediaRepresentationKind.TEXT_EMBEDDING,
                input_kinds=frozenset({MediaRepresentationKind.EXTRACTED_TEXT}),
            ),
        ]
    )
    waves = planner.plan(available_input_kinds=frozenset({MediaRepresentationKind.EXTRACTED_TEXT}))
    assert len(waves) == 1


def test_downstream_of_returns_dependent_nodes():
    embed = PipelineNodeSpec(
        stage=PipelineStage.EMBED_TEXT,
        output_kind=MediaRepresentationKind.TEXT_EMBEDDING,
        input_kinds=frozenset({MediaRepresentationKind.EXTRACTED_TEXT}),
    )
    planner = PipelineDAGPlanner(
        [
            PipelineNodeSpec(
                stage=PipelineStage.EXTRACT_TEXT, output_kind=MediaRepresentationKind.EXTRACTED_TEXT
            ),
            embed,
        ]
    )
    downstream = planner.downstream_of(MediaRepresentationKind.EXTRACTED_TEXT)
    assert downstream == (embed,)


def test_node_spec_from_pipeline_definition_maps_stage_and_inputs():
    """MediaPipelineDefinition.stage/input_kinds drive PipelineNodeSpec directly,
    reconciling the gap this module previously worked around with a local
    hand-built PipelineNodeSpec per pipeline registration.
    """
    definition = MediaPipelineDefinition(
        id="ocr_default",
        name="Default OCR",
        stage=PipelineStage.OCR,
        accepted_mime_patterns=["image/*"],
        input_kinds=[MediaRepresentationKind.METADATA],
        representation_kinds_produced=[MediaRepresentationKind.OCR_TEXT],
        producer_type=MediaProducerType.MODEL_BACKED,
        model_identity="tesseract-5.3.0",
    )

    specs = node_spec_from_pipeline_definition(definition)

    assert len(specs) == 1
    spec = specs[0]
    assert spec.stage == PipelineStage.OCR
    assert spec.output_kind == MediaRepresentationKind.OCR_TEXT
    assert spec.input_kinds == frozenset({MediaRepresentationKind.METADATA})
    assert spec.pipeline_id == "ocr_default"
    assert spec.independent is True


def test_node_spec_from_pipeline_definition_expands_multiple_output_kinds():
    """A pipeline producing several representation kinds yields one node per kind,
    all sharing the same stage/input_kinds/pipeline_id.
    """
    definition = MediaPipelineDefinition(
        id="video_scene_pipeline",
        name="Scene Detection",
        stage=PipelineStage.DETECT_SCENES,
        accepted_mime_patterns=["video/*"],
        input_kinds=[MediaRepresentationKind.METADATA],
        representation_kinds_produced=[
            MediaRepresentationKind.SCENE,
            MediaRepresentationKind.KEYFRAME,
        ],
        producer_type=MediaProducerType.DETERMINISTIC,
    )

    specs = node_spec_from_pipeline_definition(definition, independent=False)

    assert {s.output_kind for s in specs} == {
        MediaRepresentationKind.SCENE,
        MediaRepresentationKind.KEYFRAME,
    }
    assert all(s.stage == PipelineStage.DETECT_SCENES for s in specs)
    assert all(s.pipeline_id == "video_scene_pipeline" for s in specs)
    assert all(s.independent is False for s in specs)


# =============================================================================
# Stage execution: idempotency, retry, cancellation (4.4)
# =============================================================================


def test_stage_runner_cache_hit_skips_work(cache, resource_id):
    fingerprint = _fingerprint()
    existing = _representation(resource_id, MediaRepresentationKind.EXTRACTED_TEXT, fingerprint)
    cache._registry.register_representation(existing, make_current=True)

    runner = StageRunner(cache)
    node = PipelineNodeSpec(
        stage=PipelineStage.EXTRACT_TEXT, output_kind=MediaRepresentationKind.EXTRACTED_TEXT
    )

    calls = []

    def work():
        calls.append(1)
        raise AssertionError("work() should not run on a cache hit")

    outcome = runner.run_stage(node, resource_id, fingerprint, work)

    assert outcome.status == StageOutcomeStatus.CACHE_HIT
    assert calls == []


def test_stage_runner_retries_transient_failure_then_succeeds(cache, resource_id):
    fingerprint = _fingerprint()
    runner = StageRunner(cache, max_retries=2)
    node = PipelineNodeSpec(
        stage=PipelineStage.EXTRACT_TEXT, output_kind=MediaRepresentationKind.EXTRACTED_TEXT
    )

    attempts = {"count": 0}

    def work():
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise RuntimeError("transient failure")
        return _representation(resource_id, MediaRepresentationKind.EXTRACTED_TEXT, fingerprint)

    outcome = runner.run_stage(node, resource_id, fingerprint, work)

    assert outcome.status == StageOutcomeStatus.SUCCESS
    assert outcome.attempts == 2
    assert attempts["count"] == 2


def test_stage_runner_exhausts_retries_and_reports_failure(cache, resource_id):
    fingerprint = _fingerprint()
    runner = StageRunner(cache, max_retries=1)
    node = PipelineNodeSpec(
        stage=PipelineStage.EXTRACT_TEXT, output_kind=MediaRepresentationKind.EXTRACTED_TEXT
    )

    def work():
        raise RuntimeError("permanent failure")

    outcome = runner.run_stage(node, resource_id, fingerprint, work)

    assert outcome.status == StageOutcomeStatus.FAILED
    assert outcome.attempts == 2  # initial attempt + 1 retry
    assert "permanent failure" in (outcome.error or "")


def test_stage_runner_respects_cancellation_before_work(cache, resource_id):
    fingerprint = _fingerprint()
    runner = StageRunner(cache)
    node = PipelineNodeSpec(
        stage=PipelineStage.EXTRACT_TEXT, output_kind=MediaRepresentationKind.EXTRACTED_TEXT
    )
    token = CancellationToken()
    token.cancel()

    calls = []

    def work():
        calls.append(1)
        return _representation(resource_id, MediaRepresentationKind.EXTRACTED_TEXT, fingerprint)

    outcome = runner.run_stage(node, resource_id, fingerprint, work, cancellation_token=token)

    assert outcome.status == StageOutcomeStatus.CANCELLED
    assert calls == []


def test_sibling_stages_survive_independent_failures(cache, resource_id):
    """One stage failing in a wave must not prevent unrelated siblings from succeeding."""
    fingerprint_text = _fingerprint(representation_kind=MediaRepresentationKind.EXTRACTED_TEXT)
    fingerprint_meta = _fingerprint(
        representation_kind=MediaRepresentationKind.METADATA, stage=PipelineStage.EXTRACT_METADATA
    )

    runner = StageRunner(cache, max_retries=0)
    failing_node = PipelineNodeSpec(
        stage=PipelineStage.EXTRACT_TEXT, output_kind=MediaRepresentationKind.EXTRACTED_TEXT
    )
    succeeding_node = PipelineNodeSpec(
        stage=PipelineStage.EXTRACT_METADATA, output_kind=MediaRepresentationKind.METADATA
    )

    def fingerprint_for(node):
        return fingerprint_text if node is failing_node else fingerprint_meta

    def work_for(node):
        if node is failing_node:

            def fail():
                raise RuntimeError("extract_text blew up")

            return fail

        def succeed():
            return _representation(resource_id, MediaRepresentationKind.METADATA, fingerprint_meta)

        return succeed

    outcomes = runner.run_wave(
        [failing_node, succeeding_node], resource_id, fingerprint_for, work_for
    )

    outcomes_by_stage = {o.node.stage: o for o in outcomes}
    assert outcomes_by_stage[PipelineStage.EXTRACT_TEXT].status == StageOutcomeStatus.FAILED
    assert outcomes_by_stage[PipelineStage.EXTRACT_METADATA].status == StageOutcomeStatus.SUCCESS


# =============================================================================
# Coverage aggregation (4.4) -- never overclaim full understanding
# =============================================================================


def test_aggregate_coverage_all_success_is_complete():
    node_a = PipelineNodeSpec(
        stage=PipelineStage.EXTRACT_TEXT, output_kind=MediaRepresentationKind.EXTRACTED_TEXT
    )
    node_b = PipelineNodeSpec(
        stage=PipelineStage.EXTRACT_METADATA, output_kind=MediaRepresentationKind.METADATA
    )
    from katsi_core.media.planner import StageOutcome

    outcomes = [
        StageOutcome(node=node_a, status=StageOutcomeStatus.SUCCESS),
        StageOutcome(node=node_b, status=StageOutcomeStatus.SUCCESS),
    ]
    coverage = aggregate_coverage(outcomes)
    assert coverage.is_complete is True
    assert coverage.coverage_fraction == 1.0


def test_aggregate_coverage_partial_never_reports_full_understanding():
    node_a = PipelineNodeSpec(
        stage=PipelineStage.EXTRACT_TEXT, output_kind=MediaRepresentationKind.EXTRACTED_TEXT
    )
    node_b = PipelineNodeSpec(stage=PipelineStage.OCR, output_kind=MediaRepresentationKind.OCR_TEXT)
    node_c = PipelineNodeSpec(
        stage=PipelineStage.CAPTION, output_kind=MediaRepresentationKind.IMAGE_CAPTION
    )
    node_d = PipelineNodeSpec(
        stage=PipelineStage.TRANSCRIBE, output_kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT
    )
    from katsi_core.media.planner import StageOutcome

    outcomes = [
        StageOutcome(node=node_a, status=StageOutcomeStatus.SUCCESS),
        StageOutcome(node=node_b, status=StageOutcomeStatus.SUCCESS),
        StageOutcome(node=node_c, status=StageOutcomeStatus.SUCCESS),
        StageOutcome(
            node=node_d, status=StageOutcomeStatus.FAILED, error="transcription unavailable"
        ),
    ]
    coverage = aggregate_coverage(outcomes)
    assert coverage.is_complete is False
    assert coverage.coverage_fraction == pytest.approx(0.75)
    assert "failed" in coverage.detail


def test_aggregate_coverage_empty_is_not_complete():
    coverage = aggregate_coverage([])
    assert coverage.is_complete is False
    assert coverage.coverage_fraction == 0.0


# =============================================================================
# Concurrency limits (4.5)
# =============================================================================


def test_concurrency_limiter_enforces_global_cap():
    limits = ConcurrencyLimits.uniform(global_max=1, workspace_max=5)
    limiter = ConcurrencyLimiter(limits)

    with (
        limiter.acquire("workspace-a", JobResourceClass.CPU),
        pytest.raises(ConcurrencyLimitError),
        limiter.acquire("workspace-b", JobResourceClass.CPU, blocking=False),
    ):
        pass


def test_concurrency_limiter_enforces_workspace_cap_independently_of_global():
    limits = ConcurrencyLimits.uniform(global_max=5, workspace_max=1)
    limiter = ConcurrencyLimiter(limits)

    with limiter.acquire("workspace-a", JobResourceClass.CPU):
        with (
            pytest.raises(ConcurrencyLimitError),
            limiter.acquire("workspace-a", JobResourceClass.CPU, blocking=False),
        ):
            pass
        # A different workspace is unaffected by workspace-a's cap.
        with limiter.acquire("workspace-b", JobResourceClass.CPU, blocking=False):
            pass


def test_concurrency_limiter_tracks_resource_classes_independently():
    limits = ConcurrencyLimits.uniform(global_max=1, workspace_max=1)
    limiter = ConcurrencyLimiter(limits)

    # GPU has its own budget, unaffected by the CPU slot being held.
    with (
        limiter.acquire("workspace-a", JobResourceClass.CPU),
        limiter.acquire("workspace-a", JobResourceClass.GPU, blocking=False),
    ):
        pass


def test_concurrency_limiter_releases_slot_after_context_exit():
    limits = ConcurrencyLimits.uniform(global_max=1, workspace_max=1)
    limiter = ConcurrencyLimiter(limits)

    with limiter.acquire("workspace-a", JobResourceClass.MEDIA):
        pass

    # Slot released; a second acquire should succeed without blocking.
    with limiter.acquire("workspace-a", JobResourceClass.MEDIA, blocking=False):
        pass
