"""Owner-configured media pipeline registry.

The registry holds `MediaPipelineDefinition` entries configured by the
workspace owner. Agents select a registered pipeline by id or request a
representation kind; they never supply an executable, model identity, or
shell command. This module only manages pipeline *definitions* and
selection -- actual bounded execution lives in `execution.py`.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from katsi_core.media.contracts import MediaPipelineDefinition, MediaRepresentationKind
from katsi_core.media.protocols import MediaPipelineProtocol

_PROHIBITED_PIPELINE_TERMS = (
    "face identity",
    "facial recognition",
    "voice identity",
    "speaker identity",
    "emotion inference",
    "emotion recognition",
)


class PipelineRegistrationError(Exception):
    """Raised when a pipeline definition cannot be registered."""


class PipelineNotFoundError(Exception):
    """Raised when a requested pipeline id is not registered."""


def _validate_definition(definition: MediaPipelineDefinition) -> None:
    """Reject pipeline definitions that violate the security policy.

    Fixed executable/model identity, no shell, and (for deterministic
    pipelines) a resolvable executable are required before a definition
    can ever be registered. This is the single gate agents cannot bypass:
    only definitions that pass this check become selectable.
    """
    if definition.shell_enabled:
        raise PipelineRegistrationError(
            f"Pipeline '{definition.id}' must not enable shell execution"
        )
    if not definition.network_disabled:
        raise PipelineRegistrationError(f"Pipeline '{definition.id}' must disable network access")
    declared = " ".join(
        [definition.id, definition.name, definition.description, definition.model_identity or ""]
    ).lower()
    if any(term in declared for term in _PROHIBITED_PIPELINE_TERMS):
        raise PipelineRegistrationError(
            f"Pipeline '{definition.id}' requests a prohibited identity or emotion capability"
        )

    if not definition.accepted_mime_patterns:
        raise PipelineRegistrationError(
            f"Pipeline '{definition.id}' must declare at least one accepted MIME pattern"
        )

    if not definition.representation_kinds_produced:
        raise PipelineRegistrationError(
            f"Pipeline '{definition.id}' must declare at least one produced representation kind"
        )

    if definition.executable_path is None and definition.model_identity is None:
        raise PipelineRegistrationError(
            f"Pipeline '{definition.id}' must declare a fixed executable_path or model_identity"
        )

    if definition.timeout_seconds <= 0:
        raise PipelineRegistrationError(f"Pipeline '{definition.id}' must have a positive timeout")

    if definition.max_output_bytes <= 0:
        raise PipelineRegistrationError(
            f"Pipeline '{definition.id}' must have a positive max_output_bytes"
        )


@dataclass
class RegisteredPipeline:
    """A registered pipeline definition paired with its optional adapter."""

    definition: MediaPipelineDefinition
    adapter_class: type[MediaPipelineProtocol] | None = None


@dataclass
class MediaPipelineRegistry:
    """Owner-configured catalog of media processing pipeline definitions.

    Definitions are the only source of executable/model identity, fixed
    argument templates, environment policy, and resource budgets. Nothing
    in this registry accepts agent-supplied commands.
    """

    _pipelines: dict[str, RegisteredPipeline] = field(default_factory=dict)

    def register(
        self,
        definition: MediaPipelineDefinition,
        adapter_class: type[MediaPipelineProtocol] | None = None,
    ) -> None:
        """Register an owner-configured pipeline definition.

        Args:
            definition: Complete, owner-authored pipeline definition.
            adapter_class: Optional concrete `MediaPipelineProtocol` adapter.

        Raises:
            PipelineRegistrationError: If the definition is missing required
                fields or violates the shell/executable security policy, or
                if a pipeline with the same id is already registered.
        """
        if definition.id in self._pipelines:
            raise PipelineRegistrationError(f"Pipeline id '{definition.id}' is already registered")

        _validate_definition(definition)

        self._pipelines[definition.id] = RegisteredPipeline(
            definition=definition, adapter_class=adapter_class
        )

    def unregister(self, pipeline_id: str) -> None:
        """Remove a pipeline definition from the registry."""
        self._pipelines.pop(pipeline_id, None)

    def get(self, pipeline_id: str) -> RegisteredPipeline:
        """Look up a registered pipeline by id.

        Raises:
            PipelineNotFoundError: If no pipeline with that id is registered.
        """
        registered = self._pipelines.get(pipeline_id)
        if registered is None:
            raise PipelineNotFoundError(f"No pipeline registered with id '{pipeline_id}'")
        return registered

    def list_pipeline_ids(self) -> list[str]:
        """List all registered pipeline ids."""
        return sorted(self._pipelines)

    def find_for_mime_type(self, mime_type: str) -> list[RegisteredPipeline]:
        """Find registered pipelines that accept the given MIME type.

        Matching uses glob patterns against `accepted_mime_patterns`
        (e.g. "image/*" matches "image/png").
        """
        matches: list[RegisteredPipeline] = []
        for registered in self._pipelines.values():
            for pattern in registered.definition.accepted_mime_patterns:
                if fnmatch.fnmatch(mime_type, pattern):
                    matches.append(registered)
                    break
        return matches

    def find_for_representation_kind(
        self, kind: MediaRepresentationKind
    ) -> list[RegisteredPipeline]:
        """Find registered pipelines that produce a given representation kind."""
        return [
            registered
            for registered in self._pipelines.values()
            if kind in registered.definition.representation_kinds_produced
        ]

    def resolve(self, mime_type: str, kind: MediaRepresentationKind) -> RegisteredPipeline | None:
        """Resolve the pipeline for a MIME type and desired representation kind.

        Returns the first matching registered pipeline, or None if no
        pipeline accepts the MIME type and produces the requested kind.
        This is the only selection surface agents use -- they request a
        representation kind for a described resource, never a command.
        """
        for registered in self.find_for_representation_kind(kind):
            for pattern in registered.definition.accepted_mime_patterns:
                if fnmatch.fnmatch(mime_type, pattern):
                    return registered
        return None

    def available_pipeline_ids(self) -> list[str]:
        """List ids of pipelines whose adapter reports itself available.

        Pipelines without a bound adapter class are excluded since their
        availability cannot be probed.
        """
        available: list[str] = []
        for pipeline_id, registered in self._pipelines.items():
            if registered.adapter_class is None:
                continue
            is_available, _ = registered.adapter_class.check_availability()
            if is_available:
                available.append(pipeline_id)
        return sorted(available)
