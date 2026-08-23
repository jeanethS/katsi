"""Fixed bindings for owner-configured local media executables."""

from __future__ import annotations

import fnmatch

from katsi_core.media.audio_pipeline import AudioDecodePipeline
from katsi_core.media.contracts import (
    MediaPipelineDefinition,
    MediaProcessingConfig,
    MediaRepresentationKind,
    PipelineStage,
)
from katsi_core.media.pipeline_registry import MediaPipelineRegistry, PipelineRegistrationError
from katsi_core.media.protocols import MediaPipelineProtocol
from katsi_core.media.video_pipeline import (
    SceneDetectionPipeline,
    VideoMetadataPipeline,
)

_ADAPTERS: dict[
    str, tuple[type[MediaPipelineProtocol], PipelineStage, MediaRepresentationKind, str]
] = {
    "audio_decode_ffmpeg": (
        AudioDecodePipeline,
        PipelineStage.GENERATE_PROXY,
        MediaRepresentationKind.PROXY_MEDIA,
        "audio/*",
    ),
    "video_metadata_ffprobe": (
        VideoMetadataPipeline,
        PipelineStage.EXTRACT_METADATA,
        MediaRepresentationKind.MEDIA_DESCRIPTOR,
        "video/*",
    ),
    "video_scene_detect_ffmpeg": (
        SceneDetectionPipeline,
        PipelineStage.DETECT_SCENES,
        MediaRepresentationKind.SCENE,
        "video/*",
    ),
}


def adapter_class_for(
    definition: MediaPipelineDefinition,
) -> type[MediaPipelineProtocol] | None:
    """Return an allowlisted adapter only when its declared contract matches."""
    if definition.adapter_binding is None:
        return None
    binding = _ADAPTERS.get(definition.adapter_binding)
    if binding is None:
        raise PipelineRegistrationError(
            f"Unknown media adapter binding: {definition.adapter_binding}"
        )
    adapter_class, stage, kind, mime_pattern = binding
    if definition.stage is not stage or kind not in definition.representation_kinds_produced:
        raise PipelineRegistrationError(
            f"Adapter binding '{definition.adapter_binding}' does not match its pipeline contract"
        )
    if not any(
        fnmatch.fnmatch(mime_pattern, pattern) or fnmatch.fnmatch(pattern, mime_pattern)
        for pattern in definition.accepted_mime_patterns
    ):
        raise PipelineRegistrationError(
            f"Adapter binding '{definition.adapter_binding}' does not accept its declared MIME type"
        )
    return adapter_class


def _family_enabled(config: MediaProcessingConfig, definition: MediaPipelineDefinition) -> bool:
    patterns = definition.accepted_mime_patterns
    return (
        (
            config.enable_audio_processing
            and any(pattern.startswith("audio/") for pattern in patterns)
        )
        or (
            config.enable_video_processing
            and any(pattern.startswith("video/") for pattern in patterns)
        )
        or (
            config.enable_image_processing
            and any(pattern.startswith("image/") for pattern in patterns)
        )
    )


def build_media_pipeline_registry(config: MediaProcessingConfig) -> MediaPipelineRegistry:
    """Register configured, enabled local adapters; unavailable ones stay unbound."""
    registry = MediaPipelineRegistry()
    for definition in config.pipelines:
        adapter_class = adapter_class_for(definition)
        if adapter_class is not None and _family_enabled(config, definition):
            registry.register(definition, adapter_class)
    return registry
