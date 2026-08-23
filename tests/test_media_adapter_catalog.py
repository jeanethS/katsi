import pytest

from katsi_core.config import Settings, reset_settings
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


def _image_ocr_definition() -> MediaPipelineDefinition:
    return MediaPipelineDefinition(
        id="owner-ocr",
        adapter_binding="image_ocr_tesseract",
        name="Owner OCR",
        stage=PipelineStage.OCR,
        accepted_mime_patterns=["image/*"],
        representation_kinds_produced=[MediaRepresentationKind.OCR_TEXT],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path="/usr/local/bin/ocr-wrapper",
        fixed_args=["{input_path}", "{output_path}", "--lang", "spa+eng"],
    )


def _image_thumbnail_definition() -> MediaPipelineDefinition:
    return MediaPipelineDefinition(
        id="owner-thumbnail",
        adapter_binding="image_thumbnail_magick",
        name="Owner thumbnail",
        stage=PipelineStage.GENERATE_THUMBNAIL,
        accepted_mime_patterns=["image/*"],
        representation_kinds_produced=[MediaRepresentationKind.THUMBNAIL],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path="/opt/homebrew/bin/magick",
        fixed_args=["{input_path}", "-auto-orient", "-resize", "512x512>", "{output_path}"],
    )


def test_image_ocr_binding_builds_definition_bound_adapter(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KATSI_STORE__DATA_DIR", str(tmp_path))
    reset_settings()
    definition = _image_ocr_definition()

    registry = build_media_pipeline_registry(
        MediaProcessingConfig(enable_image_processing=True, pipelines=[definition])
    )
    adapter = registry.get(definition.id).build_adapter()

    from katsi_core.media.adapter_catalog import ConfiguredImageOcrPipeline

    assert isinstance(adapter, ConfiguredImageOcrPipeline)
    assert adapter.get_pipeline_definition() is definition
    assert adapter.get_pipeline_definition().fixed_args[-1] == "spa+eng"


def test_image_thumbnail_binding_builds_blob_backed_adapter(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KATSI_STORE__DATA_DIR", str(tmp_path))
    reset_settings()
    definition = _image_thumbnail_definition()

    registry = build_media_pipeline_registry(
        MediaProcessingConfig(enable_image_processing=True, pipelines=[definition])
    )
    adapter = registry.get(definition.id).build_adapter()

    assert adapter._blob_store is not None
    assert (tmp_path / "blobs").is_dir()


def test_image_binding_rejects_stage_mismatch() -> None:
    definition = _image_ocr_definition().model_copy(
        update={"stage": PipelineStage.GENERATE_THUMBNAIL}
    )

    with pytest.raises(PipelineRegistrationError, match="does not match"):
        adapter_class_for(definition)
