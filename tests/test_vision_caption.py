"""Unit tests for first-party video keyframe captioning packaging."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from katsi_core.media.contracts import (
    MediaRepresentationKind,
    MediaRepresentationStatus,
    TimeRangeLocator,
    VideoFrameLocator,
)
from katsi_core.media.settings import MediaSamplingSettings
from katsi_core.media import vision_caption


def test_sample_keyframe_timestamps_are_interior_and_capped() -> None:
    points = vision_caption.sample_keyframe_timestamps(10_000, max_frames=3)
    assert points == [2500, 5000, 7500]
    assert all(0 < p < 10_000 for p in points)
    assert vision_caption.sample_keyframe_timestamps(0, 3) == []
    # A clip shorter (in ms) than the frame cap never asks for more frames than
    # it has milliseconds, so timestamps stay unique and interior.
    assert vision_caption.sample_keyframe_timestamps(2, 5) == [1, 1]  # capped to duration


class _FakeCaptioner(vision_caption.VisionCaptioner):
    def __init__(self, text: str) -> None:
        super().__init__(model="fake-vl", host="http://x", timeout=1.0)
        self._text = text

    def caption(self, image_path: Path) -> str:  # noqa: ARG002
        return self._text


def test_caption_video_packages_one_rep_per_frame(monkeypatch, tmp_path) -> None:
    # Every frame "extracts" successfully by writing a stub file.
    def fake_extract(video_path, timestamp_ms, ffmpeg_path, out_path):  # noqa: ANN001, ARG001
        Path(out_path).write_bytes(b"jpg")
        return True

    monkeypatch.setattr(vision_caption, "_extract_frame", fake_extract)
    rvid = uuid4()
    reps = vision_caption.caption_video(
        tmp_path / "clip.mp4",
        resource_version_id=rvid,
        content_hash="a" * 64,
        duration_ms=9_000,
        ffmpeg_path="/bin/true",
        captioner=_FakeCaptioner("A mountain landscape at dusk."),
        working_dir=tmp_path,
        settings=MediaSamplingSettings(),
        max_frames=3,
    )
    assert len(reps) == 3
    for index, rep in enumerate(reps):
        assert rep.kind is MediaRepresentationKind.IMAGE_CAPTION
        assert rep.status is MediaRepresentationStatus.CURRENT
        assert rep.textual_payload == "A mountain landscape at dusk."
        assert rep.resource_version_id == rvid
        kinds = {type(loc) for loc in rep.locators}
        assert VideoFrameLocator in kinds and TimeRangeLocator in kinds
        frame_loc = next(loc for loc in rep.locators if isinstance(loc, VideoFrameLocator))
        assert frame_loc.frame_index == index


def test_caption_video_skips_failed_extractions(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(vision_caption, "_extract_frame", lambda *a, **k: False)
    reps = vision_caption.caption_video(
        tmp_path / "clip.mp4",
        resource_version_id=uuid4(),
        content_hash="b" * 64,
        duration_ms=9_000,
        ffmpeg_path="/bin/true",
        captioner=_FakeCaptioner("ignored"),
        working_dir=tmp_path,
        settings=MediaSamplingSettings(),
        max_frames=3,
    )
    assert reps == []


def test_caption_video_skips_empty_captions(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        vision_caption,
        "_extract_frame",
        lambda video_path, ts, ff, out: (Path(out).write_bytes(b"x"), True)[1],
    )
    reps = vision_caption.caption_video(
        tmp_path / "clip.mp4",
        resource_version_id=uuid4(),
        content_hash="c" * 64,
        duration_ms=9_000,
        ffmpeg_path="/bin/true",
        captioner=_FakeCaptioner("   "),
        working_dir=tmp_path,
        settings=MediaSamplingSettings(),
        max_frames=3,
    )
    assert reps == []
