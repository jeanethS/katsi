"""Governed materialisation of exported derived-media artifacts.

This boundary accepts only the closed Change Set operation models.  A caller
injects the owner-configured materializer; no operation can carry a command,
URL, or arbitrary processor arguments.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from katsi_core.media.contracts import MediaRepresentationKind
from katsi_core.media.pipeline_registry import MediaPipelineRegistry
from katsi_core.workspace.contracts import (
    DerivedMediaOperationBase,
    ExportKeyframesOperation,
    ExportTranscriptOrOcrOperation,
    GenerateProxyMediaOperation,
    GenerateThumbnailOperation,
    Operation,
    ReplaceDerivedMediaArtifactOperation,
)
from katsi_core.workspace.staging import AdjacentStagingManager


class DerivedMediaOperationError(ValueError):
    """A derived-media operation failed its closed-catalog preconditions."""


@dataclass(frozen=True)
class MaterializedMediaArtifact:
    """Validated bytes returned by an owner-bound media materializer."""

    content: bytes
    media_type: str
    pipeline_fingerprint: str
    source_relationship: str


MediaMaterializer = Callable[[DerivedMediaOperationBase], MaterializedMediaArtifact]


def _required_kind(operation: DerivedMediaOperationBase) -> MediaRepresentationKind | None:
    if isinstance(operation, GenerateThumbnailOperation):
        return MediaRepresentationKind.THUMBNAIL
    if isinstance(operation, GenerateProxyMediaOperation):
        return MediaRepresentationKind.PROXY_MEDIA
    if isinstance(operation, ExportKeyframesOperation):
        return MediaRepresentationKind.KEYFRAME
    if isinstance(operation, ExportTranscriptOrOcrOperation):
        return MediaRepresentationKind(operation.representation_kind)
    return None


@dataclass
class DerivedMediaArtifactExecutor:
    """Stages, validates, and atomically exports derived media only.

    The source resource is never opened for writing.  Exact-hash replacement
    is available solely for a previously-derived target and still creates a
    new staged output before atomic replacement.
    """

    registry: MediaPipelineRegistry
    staging: AdjacentStagingManager
    materialize: MediaMaterializer
    _staged: dict[Path, MaterializedMediaArtifact] = field(default_factory=dict)

    @staticmethod
    def supports(operation: Operation) -> bool:
        return isinstance(operation, DerivedMediaOperationBase)

    def stage(self, operation: DerivedMediaOperationBase, target_path: Path) -> dict[str, str]:
        registered = self.registry.get(operation.pipeline_id)
        required_kind = _required_kind(operation)
        if (
            required_kind is not None
            and required_kind not in registered.definition.representation_kinds_produced
        ):
            raise DerivedMediaOperationError(
                f"Pipeline '{operation.pipeline_id}' cannot produce {required_kind.value}"
            )

        artifact = self.materialize(operation)
        self._validate_artifact(operation, artifact, target_path)
        self.staging.stage_content(target_path, artifact.content)
        self._staged[target_path] = artifact
        return {
            "staged": "true",
            "pipeline_id": operation.pipeline_id,
            "pipeline_fingerprint": artifact.pipeline_fingerprint,
            "source_resource_id": str(operation.source_resource_id),
            "source_resource_version_id": str(operation.source_resource_version_id),
            "source_relationship": artifact.source_relationship,
            "output_hash": operation.expected_output_hash,
            "output_media_type": artifact.media_type,
        }

    def commit(self, operation: DerivedMediaOperationBase, target_path: Path) -> dict[str, str]:
        artifact = self._staged.pop(target_path, None)
        if artifact is None:
            raise DerivedMediaOperationError("Derived-media output was not staged")

        if isinstance(operation, ReplaceDerivedMediaArtifactOperation):
            if not target_path.exists():
                raise DerivedMediaOperationError(
                    "Derived artifact replacement target does not exist"
                )
            actual = hashlib.sha256(target_path.read_bytes()).hexdigest()
            if actual != operation.expected_current_hash:
                raise DerivedMediaOperationError("Derived artifact exact-hash precondition failed")

        self.staging.atomic_replace(self.staging.get_stage_path(target_path), target_path)
        return {
            "replaced": "true",
            "path": str(target_path),
            "pipeline_id": operation.pipeline_id,
            "pipeline_fingerprint": artifact.pipeline_fingerprint,
            "source_relationship": artifact.source_relationship,
            "output_hash": operation.expected_output_hash,
            "verification": "sha256_and_media_type_validated",
        }

    @staticmethod
    def _validate_artifact(
        operation: DerivedMediaOperationBase,
        artifact: MaterializedMediaArtifact,
        _target_path: Path,
    ) -> None:
        if len(artifact.content) > operation.max_output_bytes:
            raise DerivedMediaOperationError("Derived-media output exceeds operation limit")
        if artifact.media_type != operation.expected_output_media_type:
            raise DerivedMediaOperationError(
                "Derived-media output media type does not match declaration"
            )
        if artifact.pipeline_fingerprint != operation.pipeline_fingerprint:
            raise DerivedMediaOperationError(
                "Pipeline fingerprint does not match immutable operation"
            )
        if artifact.source_relationship != operation.source_relationship:
            raise DerivedMediaOperationError(
                "Derived artifact source relationship does not match declaration"
            )
        actual_hash = hashlib.sha256(artifact.content).hexdigest()
        if actual_hash != operation.expected_output_hash:
            raise DerivedMediaOperationError("Derived-media output hash does not match declaration")
