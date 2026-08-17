"""Tests for the video understanding pipeline (openspec tasks.md Section 8).

Real video encoding/decoding is impractical in a unit test, so these tests
mock at the subprocess-adapter boundary (or skip it entirely for the pure
planning/aggregation functions) and exercise the planning, parsing, and
composition logic for real -- especially :class:`VideoCoveragePlanner`
(task 8.2), which is the design-highlighted decision.

Fixture scenarios required by task 8.8: silent video, speech + slides,
variable frame rate, oversized video, partial coverage, scene failure, and
interrupted processing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from katsi_core.config import MediaSamplingSettings
from katsi_core.media.contracts import (
    DerivedRepresentation,
    MediaCoverage,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    PipelineStage,
    ProducerProvenance,
    TimeRangeLocator,
)
from katsi_core.media.video_pipeline import (
    AudioTranscriptionAdapter,
    ScenePlan,
    VideoComputeClass,
    VideoCoverageBudget,
    VideoCoveragePlanner,
    VideoCoveragePolicy,
    VideoStreamInfo,
    build_scene_representations,
    caption_keyframe,
    embed_keyframe,
    parse_scene_boundaries_from_showinfo,
    parse_video_stream_metadata,
    plan_scenes_and_keyframes,
    resolve_scene_boundaries,
    transcribe_video_audio_track,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _video_info(**overrides) -> VideoStreamInfo:
    base = dict(
        duration_ms=60_000,
        width=1280,
        height=720,
        frame_rate=30.0,
        is_variable_frame_rate=False,
        codec="h264",
        container="mov,mp4,m4a,3gp,3g2,mj2",
        has_audio=True,
        audio_codec="aac",
    )
    base.update(overrides)
    return VideoStreamInfo(**base)


def _budget(**overrides) -> VideoCoverageBudget:
    base = dict(
        max_duration_ms=120_000,
        hard_max_duration_ms=600_000,
        max_keyframes=50,
        max_decoded_pixels=50 * 1280 * 720,
        max_output_bytes=50 * 150_000,
        max_wall_time_seconds=30.0,
        min_scene_interval_ms=2_000,
        max_scene_interval_ms=15_000,
    )
    base.update(overrides)
    return VideoCoverageBudget(**base)


def _sampling_settings() -> MediaSamplingSettings:
    return MediaSamplingSettings()


def _fingerprint(kind: MediaRepresentationKind) -> PipelineFingerprint:
    return PipelineFingerprint(
        source_content_hash="a" * 32,
        representation_kind=kind,
        stage=PipelineStage.TRANSCRIBE,
        adapter_name="fake_audio",
        adapter_version="1.0.0",
        sampling_fingerprint="fake-v1",
    )


def _transcript_segment(
    resource_version_id, start_ms: int, end_ms: int, text: str
) -> DerivedRepresentation:
    now = datetime.now(UTC)
    rep_id = uuid4()
    return DerivedRepresentation(
        id=rep_id,
        resource_version_id=resource_version_id,
        kind=MediaRepresentationKind.TRANSCRIPT_SEGMENT,
        media_type="text/plain",
        status=MediaRepresentationStatus.CURRENT,
        created_at=now,
        updated_at=now,
        textual_payload=text,
        locators=(
            TimeRangeLocator(
                resource_version_id=resource_version_id,
                representation_id=rep_id,
                start_ms=start_ms,
                end_ms=end_ms,
            ),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.MODEL_BACKED,
            adapter_name="fake_audio",
            adapter_version="1.0.0",
        ),
        pipeline_fingerprint=_fingerprint(MediaRepresentationKind.TRANSCRIPT_SEGMENT),
    )


def _keyframe_representation(resource_version_id, timestamp_ms: int) -> DerivedRepresentation:
    now = datetime.now(UTC)
    return DerivedRepresentation(
        id=uuid4(),
        resource_version_id=resource_version_id,
        kind=MediaRepresentationKind.KEYFRAME,
        media_type="image/jpeg",
        status=MediaRepresentationStatus.CURRENT,
        created_at=now,
        updated_at=now,
        blob_reference=f"kf-{timestamp_ms}",
        blob_hash="b" * 32,
        blob_byte_count=1234,
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="fake_keyframe",
            adapter_version="1.0.0",
        ),
        pipeline_fingerprint=_fingerprint(MediaRepresentationKind.KEYFRAME),
    )


# ---------------------------------------------------------------------------
# 8.1 metadata parsing
# ---------------------------------------------------------------------------


def test_parse_video_stream_metadata_extracts_core_fields():
    raw = {
        "format": {"duration": "10.5", "format_name": "mov,mp4,m4a"},
        "streams": [
            {
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "codec_name": "h264",
                "r_frame_rate": "30/1",
                "avg_frame_rate": "30/1",
                "duration": "10.5",
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
    info = parse_video_stream_metadata(raw)
    assert info.duration_ms == 10_500
    assert info.width == 1920
    assert info.height == 1080
    assert info.codec == "h264"
    assert info.has_audio is True
    assert info.audio_codec == "aac"
    assert info.is_variable_frame_rate is False


def test_parse_video_stream_metadata_detects_variable_frame_rate():
    raw = {
        "format": {"duration": "5.0"},
        "streams": [
            {
                "codec_type": "video",
                "width": 640,
                "height": 480,
                "r_frame_rate": "30/1",
                "avg_frame_rate": "24/1",
                "duration": "5.0",
            }
        ],
    }
    info = parse_video_stream_metadata(raw)
    assert info.is_variable_frame_rate is True


def test_parse_video_stream_metadata_silent_video_has_no_audio():
    raw = {
        "format": {"duration": "3.0"},
        "streams": [
            {
                "codec_type": "video",
                "width": 320,
                "height": 240,
                "r_frame_rate": "25/1",
                "avg_frame_rate": "25/1",
                "duration": "3.0",
            }
        ],
    }
    info = parse_video_stream_metadata(raw)
    assert info.has_audio is False
    assert info.audio_codec is None


def test_parse_video_stream_metadata_missing_streams_is_malformed():
    info = parse_video_stream_metadata({"format": {}, "streams": []})
    assert info.malformed is True
    assert info.duration_ms == 0


# ---------------------------------------------------------------------------
# 8.2 VideoCoveragePlanner -- the critical design-highlighted decision
# ---------------------------------------------------------------------------


class TestVideoCoveragePlanner:
    def test_video_within_budget_achieves_full_bounded_sampling(self):
        info = _video_info(duration_ms=30_000)
        budget = _budget()
        plan = VideoCoveragePlanner().plan(info, budget)

        assert plan.policy is VideoCoveragePolicy.BOUNDED_SAMPLING
        assert plan.coverage.is_complete is True
        assert plan.coverage.coverage_fraction == 1.0
        assert plan.planned_duration_ms == 30_000
        assert plan.planned_keyframe_count > 0
        assert plan.estimated_wall_time_seconds <= budget.max_wall_time_seconds
        assert plan.estimated_output_bytes <= budget.max_output_bytes
        assert plan.estimated_decoded_pixels <= budget.max_decoded_pixels

    def test_silent_video_still_plans_visual_coverage(self):
        info = _video_info(duration_ms=20_000, has_audio=False, audio_codec=None)
        plan = VideoCoveragePlanner().plan(info, _budget())
        assert plan.policy is VideoCoveragePolicy.BOUNDED_SAMPLING
        assert plan.planned_keyframe_count > 0

    def test_oversized_video_beyond_hard_ceiling_is_unavailable(self):
        info = _video_info(duration_ms=10_000_000)  # ~2.8 hours
        budget = _budget(hard_max_duration_ms=600_000)
        plan = VideoCoveragePlanner().plan(info, budget)
        assert plan.policy is VideoCoveragePolicy.UNAVAILABLE
        assert plan.coverage.is_complete is False
        assert plan.coverage.coverage_fraction == 0.0

    def test_oversized_video_within_escalation_window_requires_owner_approval(self):
        info = _video_info(duration_ms=200_000)
        budget = _budget(
            max_duration_ms=120_000,
            hard_max_duration_ms=600_000,
            approval_escalation_multiplier=3.0,
            allow_owner_approval=True,
        )
        plan = VideoCoveragePlanner().plan(info, budget)
        assert plan.policy is VideoCoveragePolicy.OWNER_APPROVAL_REQUIRED
        assert plan.coverage.is_complete is False
        assert plan.planned_duration_ms == 200_000

    def test_oversized_video_without_approval_allowed_is_unavailable(self):
        info = _video_info(duration_ms=200_000)
        budget = _budget(
            max_duration_ms=120_000, hard_max_duration_ms=600_000, allow_owner_approval=False
        )
        plan = VideoCoveragePlanner().plan(info, budget)
        assert plan.policy is VideoCoveragePolicy.UNAVAILABLE

    def test_never_reports_full_understanding_when_reduced_by_pixel_budget(self):
        # 4K frame: pixel budget only allows a handful of keyframes even
        # though the full duration would want many more.
        info = _video_info(duration_ms=120_000, width=3840, height=2160)
        budget = _budget(
            max_duration_ms=120_000,
            max_decoded_pixels=5 * 3840 * 2160,  # only 5 keyframes worth
            max_keyframes=100,
            max_wall_time_seconds=100.0,
            max_output_bytes=100 * 150_000,
            allowed_compute_classes=frozenset({VideoComputeClass.CPU, VideoComputeClass.GPU}),
        )
        plan = VideoCoveragePlanner().plan(info, budget)

        assert plan.policy is VideoCoveragePolicy.BOUNDED_SAMPLING
        assert plan.planned_keyframe_count == 5
        # Still spans the entire duration -- never a truncated prefix.
        assert plan.planned_duration_ms == 120_000
        # But coverage is NOT reported as complete/fully understood.
        assert plan.coverage.is_complete is False
        assert plan.coverage.coverage_fraction < 1.0

    def test_partial_coverage_reduced_by_wall_time_budget(self):
        info = _video_info(duration_ms=60_000)
        budget = _budget(max_wall_time_seconds=1.5, seconds_per_keyframe_decode=0.1)
        plan = VideoCoveragePlanner().plan(info, budget)
        assert plan.policy is VideoCoveragePolicy.BOUNDED_SAMPLING
        assert plan.coverage.is_complete is False
        assert plan.estimated_wall_time_seconds <= budget.max_wall_time_seconds

    def test_compute_class_not_permitted_requires_owner_approval(self):
        info = _video_info(width=3840, height=2160, duration_ms=10_000)
        budget = _budget(
            allowed_compute_classes=frozenset({VideoComputeClass.CPU}),
            gpu_required_pixel_threshold=1000,  # force GPU requirement
        )
        plan = VideoCoveragePlanner().plan(info, budget)
        assert plan.policy is VideoCoveragePolicy.OWNER_APPROVAL_REQUIRED
        assert plan.compute_class is VideoComputeClass.GPU

    def test_compute_class_not_permitted_and_no_approval_is_unavailable(self):
        info = _video_info(width=3840, height=2160, duration_ms=10_000)
        budget = _budget(
            allowed_compute_classes=frozenset({VideoComputeClass.CPU}),
            gpu_required_pixel_threshold=1000,
            allow_owner_approval=False,
        )
        plan = VideoCoveragePlanner().plan(info, budget)
        assert plan.policy is VideoCoveragePolicy.UNAVAILABLE

    def test_zero_duration_metadata_is_unavailable(self):
        info = _video_info(duration_ms=0)
        plan = VideoCoveragePlanner().plan(info, _budget())
        assert plan.policy is VideoCoveragePolicy.UNAVAILABLE

    def test_missing_dimensions_is_unavailable(self):
        info = _video_info(width=None, height=None)
        plan = VideoCoveragePlanner().plan(info, _budget())
        assert plan.policy is VideoCoveragePolicy.UNAVAILABLE

    def test_encrypted_video_is_unavailable(self):
        info = _video_info(encrypted=True)
        plan = VideoCoveragePlanner().plan(info, _budget())
        assert plan.policy is VideoCoveragePolicy.UNAVAILABLE

    def test_malformed_video_is_unavailable(self):
        info = _video_info(malformed=True)
        plan = VideoCoveragePlanner().plan(info, _budget())
        assert plan.policy is VideoCoveragePolicy.UNAVAILABLE

    def test_variable_frame_rate_does_not_block_planning(self):
        info = _video_info(duration_ms=45_000, is_variable_frame_rate=True, frame_rate=None)
        plan = VideoCoveragePlanner().plan(info, _budget())
        assert plan.policy is VideoCoveragePolicy.BOUNDED_SAMPLING

    def test_output_byte_budget_reduces_keyframe_count(self):
        info = _video_info(duration_ms=60_000)
        budget = _budget(max_output_bytes=3 * 150_000, max_keyframes=100)
        plan = VideoCoveragePlanner().plan(info, budget)
        assert plan.policy is VideoCoveragePolicy.BOUNDED_SAMPLING
        assert plan.planned_keyframe_count <= 3
        assert plan.coverage.is_complete is False

    def test_max_keyframes_budget_reduces_density_below_min_interval(self):
        info = _video_info(duration_ms=300_000)  # would want 150 keyframes @2s interval
        budget = _budget(
            max_duration_ms=300_000,
            max_keyframes=10,
            max_output_bytes=10 * 150_000,
            max_decoded_pixels=10 * 1280 * 720,
            max_wall_time_seconds=60.0,
        )
        plan = VideoCoveragePlanner().plan(info, budget)
        assert plan.policy is VideoCoveragePolicy.BOUNDED_SAMPLING
        assert plan.planned_keyframe_count == 10
        assert plan.coverage.is_complete is False
        assert plan.planned_duration_ms == 300_000  # still spans the full video


# ---------------------------------------------------------------------------
# 8.3 Audio-track extraction + transcription reuse
# ---------------------------------------------------------------------------


class _FakeTranscriptionAdapter:
    """Stand-in for the Section 7 audio pipeline's expected shape."""

    def __init__(self, segments_factory):
        self._segments_factory = segments_factory

    def transcribe(
        self, audio_path, *, resource_version_id, source_content_hash, working_directory
    ):
        return self._segments_factory(resource_version_id)


def test_silent_video_transcription_is_unavailable_not_fabricated(tmp_path):
    info = _video_info(has_audio=False, audio_codec=None)
    result = transcribe_video_audio_track(
        info,
        uuid4(),
        audio_path=None,
        source_content_hash="a" * 32,
        transcription_adapter=_FakeTranscriptionAdapter(lambda rid: []),
        working_directory=tmp_path,
    )
    assert result.status == MediaRepresentationStatus.UNAVAILABLE
    assert result.segments == ()


def test_missing_adapter_is_unavailable(tmp_path):
    info = _video_info(has_audio=True)
    result = transcribe_video_audio_track(
        info,
        uuid4(),
        audio_path=tmp_path / "audio.wav",
        source_content_hash="a" * 32,
        transcription_adapter=None,
        working_directory=tmp_path,
    )
    assert result.status == MediaRepresentationStatus.UNAVAILABLE


def test_speech_plus_slides_retimes_segments_to_video_resource(tmp_path):
    info = _video_info(has_audio=True, duration_ms=30_000)
    video_resource_id = uuid4()

    def factory(_audio_resource_id):
        # Simulate segments produced against a *different* (audio) resource
        # id, as the real audio pipeline would if run against an extracted
        # audio resource -- task 8.3 requires retiming to the video's id.
        return [
            _transcript_segment(uuid4(), 0, 5_000, "hello"),
            _transcript_segment(uuid4(), 5_000, 12_000, "world"),
        ]

    result = transcribe_video_audio_track(
        info,
        video_resource_id,
        audio_path=tmp_path / "audio.wav",
        source_content_hash="a" * 32,
        transcription_adapter=_FakeTranscriptionAdapter(factory),
        working_directory=tmp_path,
    )
    assert result.status == MediaRepresentationStatus.CURRENT
    assert len(result.segments) == 2
    for seg in result.segments:
        assert seg.resource_version_id == video_resource_id
        for loc in seg.locators:
            assert loc.resource_version_id == video_resource_id


def test_extraction_produced_no_output_is_failed(tmp_path):
    info = _video_info(has_audio=True)
    result = transcribe_video_audio_track(
        info,
        uuid4(),
        audio_path=None,
        source_content_hash="a" * 32,
        transcription_adapter=_FakeTranscriptionAdapter(lambda rid: []),
        working_directory=tmp_path,
    )
    # has_audio True but no audio_path -> extraction itself failed
    assert result.status == MediaRepresentationStatus.FAILED


def test_no_segments_returned_is_partial_not_fabricated(tmp_path):
    info = _video_info(has_audio=True)
    result = transcribe_video_audio_track(
        info,
        uuid4(),
        audio_path=tmp_path / "audio.wav",
        source_content_hash="a" * 32,
        transcription_adapter=_FakeTranscriptionAdapter(lambda rid: []),
        working_directory=tmp_path,
    )
    assert result.status == MediaRepresentationStatus.PARTIAL
    assert result.segments == ()


def test_audio_transcription_adapter_protocol_is_structural():
    assert isinstance(_FakeTranscriptionAdapter(lambda rid: []), AudioTranscriptionAdapter)


# ---------------------------------------------------------------------------
# 8.4 Scene-boundary detection with maximum-interval fallback
# ---------------------------------------------------------------------------


def test_resolve_scene_boundaries_fills_gaps_with_max_interval():
    boundaries = resolve_scene_boundaries([10_000], duration_ms=40_000, max_interval_ms=15_000)
    assert boundaries[0] == 0
    assert boundaries[-1] == 40_000
    for a, b in zip(boundaries, boundaries[1:], strict=False):
        assert b - a <= 15_000


def test_resolve_scene_boundaries_scene_detection_failure_falls_back_to_uniform():
    # Empty detector output (task 8.8 "scene failure" scenario): the whole
    # duration must still be covered by uniform max-interval sampling.
    boundaries = resolve_scene_boundaries([], duration_ms=50_000, max_interval_ms=10_000)
    assert boundaries[0] == 0
    assert boundaries[-1] == 50_000
    assert len(boundaries) >= 5


def test_resolve_scene_boundaries_zero_duration():
    assert resolve_scene_boundaries([1000], duration_ms=0, max_interval_ms=5000) == (0,)


def test_resolve_scene_boundaries_ignores_out_of_range_candidates():
    boundaries = resolve_scene_boundaries(
        [-100, 0, 5_000, 999_999], duration_ms=10_000, max_interval_ms=20_000
    )
    assert boundaries == (0, 5_000, 10_000)


def test_parse_scene_boundaries_from_showinfo_extracts_pts_time():
    text = (
        "[Parsed_showinfo_1 @ 0x1] n:0 pts:0 pts_time:0.5 \n"
        "some unrelated line\n"
        "[Parsed_showinfo_1 @ 0x1] n:1 pts:100 pts_time:12.25 \n"
    )
    boundaries = parse_scene_boundaries_from_showinfo(text)
    assert boundaries == (500, 12_250)


def test_parse_scene_boundaries_from_showinfo_ignores_garbage():
    text = "pts_time:not-a-number\nrandom noise\n"
    assert parse_scene_boundaries_from_showinfo(text) == ()


def test_parse_scene_boundaries_from_showinfo_empty_on_interrupted_output():
    # Interrupted processing (task 8.8): partial/truncated stderr should
    # degrade gracefully to whatever boundaries were parsed, not raise.
    text = "[Parsed_showinfo_1 @ 0x1] n:0 pts_time:1.0\n[Parsed_showinfo_1 @ 0x1] n:1 pts_ti"
    boundaries = parse_scene_boundaries_from_showinfo(text)
    assert boundaries == (1000,)


# ---------------------------------------------------------------------------
# 8.5 Keyframe extraction planning (source-scene relationships)
# ---------------------------------------------------------------------------


def test_plan_scenes_and_keyframes_pairs_consecutive_boundaries():
    from katsi_core.media.video_pipeline import VideoCoveragePlan

    boundaries = (0, 5_000, 10_000, 15_000)
    plan = VideoCoveragePlan(
        policy=VideoCoveragePolicy.BOUNDED_SAMPLING,
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        planned_duration_ms=15_000,
        planned_keyframe_count=10,
    )
    scenes = plan_scenes_and_keyframes(boundaries, plan)
    assert len(scenes) == 3
    assert scenes[0] == ScenePlan(start_ms=0, end_ms=5_000, keyframe_timestamp_ms=2_500)
    assert scenes[-1].end_ms == 15_000


def test_plan_scenes_and_keyframes_respects_keyframe_budget_spread_across_video():
    from katsi_core.media.video_pipeline import VideoCoveragePlan

    boundaries = tuple(range(0, 100_001, 5_000))  # 20 scenes
    plan = VideoCoveragePlan(
        policy=VideoCoveragePolicy.BOUNDED_SAMPLING,
        coverage=MediaCoverage(is_complete=False, coverage_fraction=0.25),
        planned_duration_ms=100_000,
        planned_keyframe_count=5,
    )
    scenes = plan_scenes_and_keyframes(boundaries, plan)
    assert len(scenes) == 5
    # Selected scenes must span the whole timeline, not just the prefix.
    assert scenes[0].start_ms < 20_000
    assert scenes[-1].end_ms > 80_000


def test_plan_scenes_and_keyframes_empty_boundaries_yields_no_scenes():
    from katsi_core.media.video_pipeline import VideoCoveragePlan

    plan = VideoCoveragePlan(
        policy=VideoCoveragePolicy.UNAVAILABLE,
        coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0),
    )
    assert plan_scenes_and_keyframes((), plan) == ()
    assert plan_scenes_and_keyframes((0,), plan) == ()


# ---------------------------------------------------------------------------
# 8.6 Optional keyframe captions and visual embeddings
# ---------------------------------------------------------------------------


def test_caption_keyframe_returns_none_without_adapter(tmp_path):
    keyframe = _keyframe_representation(uuid4(), 1000)
    result = caption_keyframe(
        None, keyframe, b"jpeg-bytes", source_content_hash="a" * 32, working_directory=tmp_path
    )
    assert result is None


def test_embed_keyframe_returns_none_without_adapter(tmp_path):
    keyframe = _keyframe_representation(uuid4(), 1000)
    result = embed_keyframe(
        None, keyframe, b"jpeg-bytes", source_content_hash="a" * 32, working_directory=tmp_path
    )
    assert result is None


def test_caption_keyframe_uses_adapter_when_present(tmp_path):
    keyframe = _keyframe_representation(uuid4(), 1000)

    class _FakeCaptionAdapter:
        def caption(
            self, image_bytes, *, resource_version_id, source_content_hash, working_directory
        ):
            now = datetime.now(UTC)
            return DerivedRepresentation(
                id=uuid4(),
                resource_version_id=resource_version_id,
                kind=MediaRepresentationKind.IMAGE_CAPTION,
                media_type="text/plain",
                status=MediaRepresentationStatus.CURRENT,
                created_at=now,
                updated_at=now,
                textual_payload="a caption",
                coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
                producer=ProducerProvenance(
                    producer_type=MediaProducerType.MODEL_BACKED,
                    adapter_name="fake_caption",
                    adapter_version="1.0.0",
                ),
                pipeline_fingerprint=_fingerprint(MediaRepresentationKind.IMAGE_CAPTION),
            )

    result = caption_keyframe(
        _FakeCaptionAdapter(), keyframe, b"jpeg-bytes",
        source_content_hash="a" * 32, working_directory=tmp_path,
    )
    assert result is not None
    assert result.textual_payload == "a caption"


def test_caption_keyframe_adapter_failure_degrades_to_none(tmp_path):
    keyframe = _keyframe_representation(uuid4(), 1000)

    class _RaisingAdapter:
        def caption(self, *args, **kwargs):
            raise RuntimeError("model unavailable")

    assert caption_keyframe(
        _RaisingAdapter(), keyframe, b"x", source_content_hash="a" * 32, working_directory=tmp_path
    ) is None


# ---------------------------------------------------------------------------
# 8.7 Scene representations combine range + keyframes + transcript evidence
# ---------------------------------------------------------------------------


def test_build_scene_representations_combines_keyframe_and_transcript():
    resource_version_id = uuid4()
    scenes = (
        ScenePlan(start_ms=0, end_ms=5_000, keyframe_timestamp_ms=2_500),
        ScenePlan(start_ms=5_000, end_ms=10_000, keyframe_timestamp_ms=7_500),
    )
    keyframes = {
        2_500: _keyframe_representation(resource_version_id, 2_500),
        7_500: _keyframe_representation(resource_version_id, 7_500),
    }
    transcript = [
        _transcript_segment(resource_version_id, 1_000, 4_000, "intro speech"),
        _transcript_segment(resource_version_id, 6_000, 8_000, "slide two narration"),
        _transcript_segment(resource_version_id, 20_000, 25_000, "later, unrelated"),
    ]

    reps = build_scene_representations(
        scenes, keyframes, transcript,
        resource_version_id=resource_version_id,
        source_content_hash="a" * 32,
        settings=_sampling_settings(),
    )

    assert len(reps) == 2
    first, second = reps
    assert first.kind == MediaRepresentationKind.SCENE
    assert first.status == MediaRepresentationStatus.CURRENT
    assert first.textual_payload == "intro speech"
    assert first.locators[0].start_ms == 0
    assert first.locators[0].end_ms == 5_000
    assert first.locators[0].keyframe_ids == (keyframes[2_500].id,)

    assert second.textual_payload == "slide two narration"
    # The unrelated later segment must not leak into either scene.
    assert "later" not in (first.textual_payload or "")
    assert "later" not in (second.textual_payload or "")


def test_build_scene_representations_missing_keyframe_is_partial_not_dropped():
    resource_version_id = uuid4()
    scenes = (ScenePlan(start_ms=0, end_ms=5_000, keyframe_timestamp_ms=2_500),)
    reps = build_scene_representations(
        scenes, {}, [],
        resource_version_id=resource_version_id,
        source_content_hash="a" * 32,
        settings=_sampling_settings(),
    )
    assert len(reps) == 1
    assert reps[0].status == MediaRepresentationStatus.PARTIAL
    assert reps[0].coverage.is_complete is False
    assert reps[0].locators[0].keyframe_ids == ()


def test_build_scene_representations_no_scenes_yields_no_representations():
    reps = build_scene_representations(
        (), {}, [],
        resource_version_id=uuid4(),
        source_content_hash="a" * 32,
        settings=_sampling_settings(),
    )
    assert reps == ()


# ---------------------------------------------------------------------------
# End-to-end style composition using only the pure planning/composition
# surface (no subprocess involved) -- proves the full task-8.8 fixture set.
# ---------------------------------------------------------------------------


def test_end_to_end_speech_plus_slides_video_scenario():
    info = _video_info(duration_ms=60_000, has_audio=True)
    budget = _budget(max_duration_ms=60_000)
    plan = VideoCoveragePlanner().plan(info, budget)
    assert plan.policy is VideoCoveragePolicy.BOUNDED_SAMPLING

    boundaries = resolve_scene_boundaries(
        [10_000, 25_000, 40_000], duration_ms=60_000, max_interval_ms=plan.scene_interval_ms
    )
    scenes = plan_scenes_and_keyframes(boundaries, plan)
    assert scenes
    assert scenes[0].start_ms == 0
    assert scenes[-1].end_ms == 60_000

    resource_version_id = uuid4()
    keyframes = {
        s.keyframe_timestamp_ms: _keyframe_representation(
            resource_version_id, s.keyframe_timestamp_ms
        )
        for s in scenes
    }
    transcript = [
        _transcript_segment(resource_version_id, 0, 9_000, "welcome slide narration"),
        _transcript_segment(resource_version_id, 26_000, 39_000, "second slide narration"),
    ]

    reps = build_scene_representations(
        scenes, keyframes, transcript,
        resource_version_id=resource_version_id,
        source_content_hash="a" * 32,
        settings=_sampling_settings(),
    )
    assert len(reps) == len(scenes)
    assert any(r.textual_payload == "welcome slide narration" for r in reps)


def test_end_to_end_interrupted_processing_scene_detection_returns_partial_boundaries():
    # Interrupted processing: only a couple of scene changes were logged
    # before the detector was killed -- fallback still spans the full video.
    info = _video_info(duration_ms=90_000)
    budget = _budget(max_duration_ms=90_000)
    plan = VideoCoveragePlanner().plan(info, budget)

    partial_detector_output = (
        "[Parsed_showinfo_1 @ 0x1] n:0 pts_time:5.0\n"
        # simulate truncation: process was killed mid-line
        "[Parsed_showinfo_1 @ 0x1] n:1 pts_ti"
    )
    detected = parse_scene_boundaries_from_showinfo(partial_detector_output)
    boundaries = resolve_scene_boundaries(
        detected, duration_ms=90_000, max_interval_ms=plan.scene_interval_ms
    )
    assert boundaries[0] == 0
    assert boundaries[-1] == 90_000
    for a, b in zip(boundaries, boundaries[1:], strict=False):
        assert b - a <= plan.scene_interval_ms
