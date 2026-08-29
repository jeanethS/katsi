"""First-party local vision captioning for video keyframes.

Captioning asks a local Ollama vision model to describe a sampled frame. Unlike
the owner-configured media pipelines -- which run under a network-denied
subprocess sandbox (see ``pipeline_registry`` and ``execution``) -- this is
first-party katsi code at the same trust level as :class:`EmbedClient`, so it
talks to the local Ollama daemon directly. A sandboxed executable could not:
the deny-network profile blocks even the loopback socket the daemon listens on.

The output is registered as one ``IMAGE_CAPTION`` representation per keyframe,
carrying both a :class:`VideoFrameLocator` (the exact frame) and a
:class:`TimeRangeLocator` (the sampled interval) so downstream search can cite
where in the clip the description came from.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from katsi_core.media.contracts import (
    DerivedRepresentation,
    MediaCoverage,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineStage,
    ProducerProvenance,
    TimeRangeLocator,
    VideoFrameLocator,
)
from katsi_core.media.fingerprint import build_pipeline_fingerprint
from katsi_core.media.settings import MediaSamplingSettings

if TYPE_CHECKING:
    import ollama

_ADAPTER_NAME = "video_keyframe_caption"
_ADAPTER_VERSION = "1.0.0"
_PROMPT = (
    "Describe this video frame in one concise English sentence for search: the "
    "scene, setting, and the key objects or subjects visible. Plain description "
    "only, no preamble."
)
_CAPTION_MAX_CHARS = 2000


def sample_keyframe_timestamps(duration_ms: int, max_frames: int) -> list[int]:
    """Evenly spaced sample points inside ``(0, duration_ms)``.

    Interior points (never 0 or the final millisecond) so ffmpeg always lands on
    a real decoded frame. ``max_frames`` caps cost -- captioning is the slow,
    expensive stage, so a clip is summarized by a few frames, not every scene.
    """
    # ponytail: even-interval sampling, not scene-aware. A fast action clip may
    # under-sample; wire scene midpoints here if recall matters more than cost.
    if duration_ms <= 0 or max_frames < 1:
        return []
    count = min(max_frames, duration_ms)
    return [round(duration_ms * (index + 1) / (count + 1)) for index in range(count)]


class VisionCaptioner:
    """Local Ollama vision model wrapper. Deferred client, like EmbedClient."""

    def __init__(
        self,
        *,
        model: str,
        host: str,
        timeout: float,
        client: ollama.Client | None = None,
    ) -> None:
        self._model = model
        self._host = host
        self._timeout = timeout
        self._client = client

    def _get_client(self) -> ollama.Client:
        if self._client is None:
            import ollama

            self._client = ollama.Client(host=self._host, timeout=self._timeout)
        return self._client

    def caption(self, image_path: Path) -> str:
        response = self._get_client().generate(
            model=self._model,
            prompt=_PROMPT,
            images=[str(image_path)],
            stream=False,
        )
        text = str(response.get("response", "")).strip()
        return text[:_CAPTION_MAX_CHARS]


def _extract_frame(video_path: Path, timestamp_ms: int, ffmpeg_path: str, out_path: Path) -> bool:
    """Decode one frame at ``timestamp_ms`` to ``out_path`` with fixed args."""
    # Args are fixed and paths are first-party (never agent-supplied), so there
    # is no injection surface; -ss before -i keeps the seek fast.
    completed = subprocess.run(  # noqa: S603 - fixed argv, first-party paths
        [
            ffmpeg_path,
            "-y",
            "-ss",
            f"{timestamp_ms / 1000.0:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(out_path),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    return completed.returncode == 0 and out_path.exists()


def caption_video(
    video_path: Path,
    *,
    resource_version_id: UUID,
    content_hash: str,
    duration_ms: int,
    ffmpeg_path: str,
    captioner: VisionCaptioner,
    working_dir: Path,
    settings: MediaSamplingSettings,
    max_frames: int = 3,
) -> list[DerivedRepresentation]:
    """Sample, caption, and package keyframe captions for one video.

    Returns one ``IMAGE_CAPTION`` representation per successfully captioned
    frame. A frame whose extraction or captioning fails is skipped, never
    fabricated -- partial coverage is honest coverage.
    """
    timestamps = sample_keyframe_timestamps(duration_ms, max_frames)
    representations: list[DerivedRepresentation] = []
    fingerprint = build_pipeline_fingerprint(
        source_content_hash=content_hash,
        representation_kind=MediaRepresentationKind.IMAGE_CAPTION,
        stage=PipelineStage.CAPTION,
        adapter_name=_ADAPTER_NAME,
        adapter_version=_ADAPTER_VERSION,
        settings=settings,
        model_identity=captioner._model,
        prompt_version="v1",
    )
    now = datetime.now(UTC)
    for index, timestamp_ms in enumerate(timestamps):
        frame_path = working_dir / f"kf_{resource_version_id}_{timestamp_ms}.jpg"
        if not _extract_frame(video_path, timestamp_ms, ffmpeg_path, frame_path):
            continue
        caption = captioner.caption(frame_path).strip()
        if not caption:
            continue
        rep_id = uuid4()
        rvid = resource_version_id
        representations.append(
            DerivedRepresentation(
                id=rep_id,
                resource_version_id=rvid,
                kind=MediaRepresentationKind.IMAGE_CAPTION,
                media_type="text/plain",
                status=MediaRepresentationStatus.CURRENT,
                created_at=now,
                updated_at=now,
                textual_payload=caption,
                locators=(
                    VideoFrameLocator(
                        resource_version_id=rvid,
                        representation_id=rep_id,
                        timestamp_ms=timestamp_ms,
                        frame_index=index,
                    ),
                    TimeRangeLocator(
                        resource_version_id=rvid,
                        representation_id=rep_id,
                        start_ms=timestamp_ms,
                        end_ms=min(timestamp_ms + 1, duration_ms),
                    ),
                ),
                coverage=MediaCoverage(
                    is_complete=False,
                    coverage_fraction=len(timestamps) and 1.0 / len(timestamps),
                    detail=f"keyframe {index + 1} of {len(timestamps)}",
                ),
                confidence=None,
                producer=ProducerProvenance(
                    producer_type=MediaProducerType.MODEL_BACKED,
                    adapter_name=_ADAPTER_NAME,
                    adapter_version=_ADAPTER_VERSION,
                    model_identity=captioner._model,
                ),
                pipeline_fingerprint=fingerprint,
            )
        )
    return representations
