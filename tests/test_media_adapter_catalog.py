import pytest

from katsi_core.config import Settings
from katsi_core.media.adapter_catalog import adapter_class_for, build_media_pipeline_registry
from katsi_core.media.contracts import (
    MediaPipelineDefinition,
    MediaProcessingConfig,
    MediaProducerType,
    MediaRepresentationKind,
    PipelineStage,
)
from katsi_core.media.pipeline_registry import PipelineRegistrationError
from katsi_core.media.video_pipeline import SceneDetectionPipeline


def _scene_definition() -> MediaPipelineDefinition:
    return MediaPipelineDefinition(
        id="owner-scene",
        adapter_binding="video_scene_detect_ffmpeg",
        name="Owner scene detector",
        stage=PipelineStage.DETECT_SCENES,
        accepted_mime_patterns=["video/*"],
        representation_kinds_produced=[MediaRepresentationKind.SCENE],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path="/opt/local/bin/ffmpeg",
    )


def test_media_configuration_is_disabled_by_default() -> None:
    settings = Settings()

    assert settings.media.pipelines == []
    assert settings.media.enable_video_processing is False


def test_settings_loads_media_configuration(tmp_path) -> None:
    config_path = tmp_path / "katsi.toml"
    config_path.write_text(
        """
[katsi.media]
enable_video_processing = true

[[katsi.media.pipelines]]
id = "owner-scene"
adapter_binding = "video_scene_detect_ffmpeg"
name = "Owner scene detector"
stage = "detect_scenes"
accepted_mime_patterns = ["video/*"]
representation_kinds_produced = ["scene"]
producer_type = "deterministic"
executable_path = "/usr/bin/ffmpeg"
"""
    )

    settings = Settings.load(config_path)

    assert settings.media.enable_video_processing is True
    assert settings.media.pipelines[0].adapter_binding == "video_scene_detect_ffmpeg"


def test_owner_configured_pipeline_registers_a_known_adapter() -> None:
    definition = _scene_definition()

    registry = build_media_pipeline_registry(
        MediaProcessingConfig(enable_video_processing=True, pipelines=[definition])
    )

    assert registry.get(definition.id).adapter_class is SceneDetectionPipeline


def test_unknown_binding_is_rejected() -> None:
    definition = _scene_definition().model_copy(update={"adapter_binding": "not-a-pipeline"})

    with pytest.raises(PipelineRegistrationError, match="Unknown"):
        adapter_class_for(definition)


def test_disabled_or_unavailable_pipeline_is_not_available() -> None:
    definition = _scene_definition().model_copy(update={"executable_path": "/does/not/exist"})

    disabled = build_media_pipeline_registry(MediaProcessingConfig(pipelines=[definition]))
    enabled = build_media_pipeline_registry(
        MediaProcessingConfig(enable_video_processing=True, pipelines=[definition])
    )

    assert disabled.list_pipeline_ids() == []
    assert enabled.get(definition.id).is_available() == (
        False,
        "Configured executable is unavailable: /does/not/exist",
    )
