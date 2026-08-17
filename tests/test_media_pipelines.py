"""Tests for the media pipeline registry and bounded subprocess execution.

These tests prove the core security invariant of the media pipeline system:
agents can only ever select an owner-registered pipeline definition or
request a representation kind. There is no code path through which an
agent-supplied or model-supplied string can be executed as a shell command.
"""

from __future__ import annotations

import shutil
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from katsi_core.media.contracts import (
    DerivedRepresentation,
    MediaCoverage,
    MediaPipelineDefinition,
    MediaProducerType,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    PipelineStage,
    ProducerProvenance,
    WholeResourceLocator,
)
from katsi_core.media.execution import (
    BoundedSubprocessExecutor,
    PipelineExecutionOrchestrator,
    SubprocessSecurityError,
    validate_json_output,
)
from katsi_core.media.pipeline_registry import (
    MediaPipelineRegistry,
    PipelineNotFoundError,
    PipelineRegistrationError,
)
from katsi_core.media.protocols import (
    HardwareRequirement,
    MediaPipelineProtocol,
    SoftwareDependency,
)

# ---------------------------------------------------------------------------
# Fake adapters (task 3.7): prove the registry/executor never take an
# agent-supplied command, only a registered definition + fixed template.
# ---------------------------------------------------------------------------


def _definition(**overrides: Any) -> MediaPipelineDefinition:
    base = dict(
        id="fake_ocr_v1",
        name="Fake OCR",
        stage=PipelineStage.OCR,
        accepted_mime_patterns=["image/*"],
        representation_kinds_produced=[MediaRepresentationKind.OCR_TEXT],
        producer_type=MediaProducerType.DETERMINISTIC,
        executable_path=shutil.which("echo") or "/bin/echo",
        fixed_args=["fixed-safe-output"],
        allowed_env_vars=[],
        shell_enabled=False,
        network_disabled=True,
        timeout_seconds=5.0,
        max_output_bytes=4096,
        retry_on_failure=True,
        strict_output_contract=True,
    )
    base.update(overrides)
    return MediaPipelineDefinition(**base)


def _fingerprint(
    kind: MediaRepresentationKind = MediaRepresentationKind.OCR_TEXT,
) -> PipelineFingerprint:
    return PipelineFingerprint(
        source_content_hash="a" * 32,
        representation_kind=kind,
        stage=PipelineStage.OCR,
        adapter_name="fake_ocr_v1",
        adapter_version="1.0.0",
        sampling_fingerprint="fake-v1",
    )


def _representation(
    resource_version_id, kind: MediaRepresentationKind, status: MediaRepresentationStatus, text: str
) -> DerivedRepresentation:
    now = datetime.now(UTC)
    rep_id = uuid4()
    return DerivedRepresentation(
        id=rep_id,
        resource_version_id=resource_version_id,
        kind=kind,
        media_type="text/plain",
        status=status,
        created_at=now,
        updated_at=now,
        textual_payload=text,
        locators=(
            WholeResourceLocator(resource_version_id=resource_version_id, representation_id=rep_id),
        ),
        coverage=MediaCoverage(is_complete=True, coverage_fraction=1.0),
        producer=ProducerProvenance(
            producer_type=MediaProducerType.DETERMINISTIC,
            adapter_name="fake_ocr_v1",
            adapter_version="1.0.0",
        ),
        pipeline_fingerprint=_fingerprint(kind),
    )


class FakeAlwaysValidAdapter(MediaPipelineProtocol):
    """Fake adapter that never touches subprocess and always validates."""

    @classmethod
    def get_adapter_name(cls) -> str:
        return "fake_always_valid"

    @classmethod
    def get_adapter_version(cls) -> str:
        return "1.0.0"

    @classmethod
    def get_pipeline_definition(cls) -> MediaPipelineDefinition:
        return _definition()

    @classmethod
    def get_hardware_requirements(cls) -> list[HardwareRequirement]:
        return [HardwareRequirement.NONE]

    @classmethod
    def get_software_dependencies(cls) -> list[SoftwareDependency]:
        return [SoftwareDependency.NONE]

    def process(
        self,
        file_path,
        resource_version_id,
        source_content_hash,
        pipeline_fingerprint,
        working_directory,
    ):
        return _representation(
            resource_version_id,
            pipeline_fingerprint.representation_kind,
            MediaRepresentationStatus.CURRENT,
            "hello",
        )

    def validate_output(self, output, representation_kind):
        if output.status != MediaRepresentationStatus.CURRENT:
            return False, "expected current status"
        return True, None


class FakeAlwaysInvalidAdapter(FakeAlwaysValidAdapter):
    """Fake adapter whose output always fails validation."""

    def __init__(self) -> None:
        self.call_count = 0

    def process(
        self,
        file_path,
        resource_version_id,
        source_content_hash,
        pipeline_fingerprint,
        working_directory,
    ):
        self.call_count += 1
        return _representation(
            resource_version_id,
            pipeline_fingerprint.representation_kind,
            MediaRepresentationStatus.CURRENT,
            "",
        )

    def validate_output(self, output, representation_kind):
        return False, "empty payload is never valid"


class FakeInvalidThenValidAdapter(FakeAlwaysValidAdapter):
    """Fake adapter that fails validation once, then succeeds on retry."""

    def __init__(self) -> None:
        self.call_count = 0

    def process(
        self,
        file_path,
        resource_version_id,
        source_content_hash,
        pipeline_fingerprint,
        working_directory,
    ):
        self.call_count += 1
        text = "" if self.call_count == 1 else "recovered"
        return _representation(
            resource_version_id,
            pipeline_fingerprint.representation_kind,
            MediaRepresentationStatus.CURRENT,
            text,
        )

    def validate_output(self, output, representation_kind):
        if not output.textual_payload:
            return False, "empty payload"
        return True, None


class FakeRaisingAdapter(FakeAlwaysValidAdapter):
    """Fake adapter that always raises, proving orchestrator handles exceptions."""

    def __init__(self) -> None:
        self.call_count = 0

    def process(
        self,
        file_path,
        resource_version_id,
        source_content_hash,
        pipeline_fingerprint,
        working_directory,
    ):
        self.call_count += 1
        raise RuntimeError("simulated adapter crash")

    def validate_output(self, output, representation_kind):
        return True, None


class FakeAgentInjectionAttemptAdapter(FakeAlwaysValidAdapter):
    """Fake adapter simulating an agent trying to smuggle a command via output.

    Even if a model/agent embeds shell-like text in a textual payload, this
    text is data, never a command; nothing in the orchestrator or executor
    interprets representation content as something to execute.
    """

    def process(
        self,
        file_path,
        resource_version_id,
        source_content_hash,
        pipeline_fingerprint,
        working_directory,
    ):
        return _representation(
            resource_version_id,
            pipeline_fingerprint.representation_kind,
            MediaRepresentationStatus.CURRENT,
            "'; rm -rf / #",
        )

    def validate_output(self, output, representation_kind):
        return True, None


# ---------------------------------------------------------------------------
# Pipeline registry tests
# ---------------------------------------------------------------------------


class TestMediaPipelineRegistry:
    def test_register_and_resolve_by_mime_and_kind(self):
        registry = MediaPipelineRegistry()
        registry.register(_definition())

        resolved = registry.resolve("image/png", MediaRepresentationKind.OCR_TEXT)

        assert resolved is not None
        assert resolved.definition.id == "fake_ocr_v1"

    def test_resolve_returns_none_for_unregistered_kind(self):
        registry = MediaPipelineRegistry()
        registry.register(_definition())

        resolved = registry.resolve("image/png", MediaRepresentationKind.VISUAL_EMBEDDING)

        assert resolved is None

    def test_duplicate_id_registration_rejected(self):
        registry = MediaPipelineRegistry()
        registry.register(_definition())

        with pytest.raises(PipelineRegistrationError):
            registry.register(_definition())

    def test_shell_enabled_definition_rejected_at_registration(self):
        registry = MediaPipelineRegistry()

        with pytest.raises(PipelineRegistrationError):
            registry.register(_definition(shell_enabled=True))

    def test_definition_without_executable_or_model_rejected(self):
        registry = MediaPipelineRegistry()

        with pytest.raises(PipelineRegistrationError):
            registry.register(_definition(executable_path=None, model_identity=None))

    def test_get_missing_pipeline_raises(self):
        registry = MediaPipelineRegistry()

        with pytest.raises(PipelineNotFoundError):
            registry.get("does_not_exist")

    def test_find_for_mime_type_matches_glob(self):
        registry = MediaPipelineRegistry()
        registry.register(_definition(accepted_mime_patterns=["image/*"]))

        matches = registry.find_for_mime_type("image/jpeg")

        assert len(matches) == 1

    def test_available_pipeline_ids_requires_adapter_binding(self):
        registry = MediaPipelineRegistry()
        registry.register(_definition(id="unbound"), adapter_class=None)
        registry.register(_definition(id="bound"), adapter_class=FakeAlwaysValidAdapter)

        available = registry.available_pipeline_ids()

        assert available == ["bound"]


# ---------------------------------------------------------------------------
# Bounded subprocess execution tests
# ---------------------------------------------------------------------------


class TestBoundedSubprocessExecutor:
    def test_shell_metacharacters_in_args_are_never_interpreted(self, tmp_path):
        """The core proof that shell=False prevents command injection.

        A fixed_args entry containing shell metacharacters must be passed
        to argv literally; if the executor ever used shell=True, this would
        create `pwned_file` in the working directory.
        """
        marker = tmp_path / "pwned_file"
        executor = BoundedSubprocessExecutor()
        definition = _definition(
            executable_path=shutil.which("echo") or "/bin/echo",
            fixed_args=[f"hello && touch {marker}"],
        )

        result = executor.execute(
            definition, input_path=tmp_path / "input.bin", working_directory=tmp_path
        )

        assert not marker.exists()
        assert "&&" in result.stdout_sample or "hello" in result.stdout_sample

    def test_refuses_to_execute_shell_enabled_definition(self, tmp_path):
        executor = BoundedSubprocessExecutor()
        definition = _definition(shell_enabled=True)

        with pytest.raises(SubprocessSecurityError):
            executor.execute(definition, input_path=tmp_path / "in.bin", working_directory=tmp_path)

    def test_refuses_to_execute_without_fixed_executable(self, tmp_path):
        executor = BoundedSubprocessExecutor()
        definition = _definition(executable_path=None, model_identity="some-model")

        with pytest.raises(SubprocessSecurityError):
            executor.execute(definition, input_path=tmp_path / "in.bin", working_directory=tmp_path)

    def test_unknown_placeholder_in_template_is_rejected(self, tmp_path):
        executor = BoundedSubprocessExecutor()
        definition = _definition(fixed_args=["{agent_supplied_command}"])

        with pytest.raises(SubprocessSecurityError):
            executor.execute(definition, input_path=tmp_path / "in.bin", working_directory=tmp_path)

    def test_input_path_placeholder_is_substituted_with_real_path(self, tmp_path):
        executor = BoundedSubprocessExecutor()
        input_file = tmp_path / "source.bin"
        input_file.write_bytes(b"data")
        definition = _definition(
            executable_path=shutil.which("echo") or "/bin/echo",
            fixed_args=["{input_path}"],
        )

        result = executor.execute(definition, input_path=input_file, working_directory=tmp_path)

        assert str(input_file) in result.stdout_sample

    def test_timeout_kills_long_running_process(self, tmp_path):
        executor = BoundedSubprocessExecutor()
        definition = _definition(
            executable_path=sys.executable,
            fixed_args=["-c", "import time; time.sleep(5)"],
            timeout_seconds=0.2,
        )

        result = executor.execute(
            definition, input_path=tmp_path / "in.bin", working_directory=tmp_path
        )

        assert result.timed_out is True

    def test_output_is_truncated_to_configured_limit(self, tmp_path):
        executor = BoundedSubprocessExecutor()
        definition = _definition(
            executable_path=sys.executable,
            fixed_args=["-c", "print('A' * 10000)"],
            max_output_bytes=16,
        )

        result = executor.execute(
            definition, input_path=tmp_path / "in.bin", working_directory=tmp_path
        )

        assert result.output_truncated is True
        assert len(result.stdout_sample.encode("utf-8")) <= 16

    def test_environment_is_sanitized_to_allowlist(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KATSI_TEST_SECRET", "should-not-leak")
        executor = BoundedSubprocessExecutor()
        definition = _definition(
            executable_path=sys.executable,
            fixed_args=["-c", "import os; print(os.environ.get('KATSI_TEST_SECRET', 'ABSENT'))"],
            allowed_env_vars=[],
        )

        result = executor.execute(
            definition, input_path=tmp_path / "in.bin", working_directory=tmp_path
        )

        assert "ABSENT" in result.stdout_sample
        assert "should-not-leak" not in result.stdout_sample

    def test_allowlisted_environment_variable_is_passed_through(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KATSI_TEST_ALLOWED", "visible-value")
        executor = BoundedSubprocessExecutor()
        definition = _definition(
            executable_path=sys.executable,
            fixed_args=["-c", "import os; print(os.environ.get('KATSI_TEST_ALLOWED', 'ABSENT'))"],
            allowed_env_vars=["KATSI_TEST_ALLOWED"],
        )

        result = executor.execute(
            definition, input_path=tmp_path / "in.bin", working_directory=tmp_path
        )

        assert "visible-value" in result.stdout_sample


# ---------------------------------------------------------------------------
# Strict output validation + retry orchestration tests (task 3.6)
# ---------------------------------------------------------------------------


class TestPipelineExecutionOrchestrator:
    def test_valid_output_is_returned_without_retry(self, tmp_path):
        adapter = FakeAlwaysValidAdapter()
        orchestrator = PipelineExecutionOrchestrator()

        result = orchestrator.run(
            adapter, _definition(), tmp_path / "in.png", uuid4(), "a" * 32, _fingerprint()
        )

        assert result.status == MediaRepresentationStatus.CURRENT

    def test_invalid_output_is_retried_once_then_recovers(self, tmp_path):
        adapter = FakeInvalidThenValidAdapter()
        orchestrator = PipelineExecutionOrchestrator()

        result = orchestrator.run(
            adapter,
            _definition(retry_on_failure=True),
            tmp_path / "in.png",
            uuid4(),
            "a" * 32,
            _fingerprint(),
        )

        assert adapter.call_count == 2
        assert result.status == MediaRepresentationStatus.CURRENT
        assert result.textual_payload == "recovered"

    def test_second_invalid_result_produces_failed_status(self, tmp_path):
        adapter = FakeAlwaysInvalidAdapter()
        orchestrator = PipelineExecutionOrchestrator()

        result = orchestrator.run(
            adapter,
            _definition(retry_on_failure=True),
            tmp_path / "in.png",
            uuid4(),
            "a" * 32,
            _fingerprint(),
        )

        assert adapter.call_count == 2
        assert result.status == MediaRepresentationStatus.FAILED
        assert result.error is not None

    def test_no_retry_when_retry_on_failure_is_false(self, tmp_path):
        adapter = FakeAlwaysInvalidAdapter()
        orchestrator = PipelineExecutionOrchestrator()

        result = orchestrator.run(
            adapter,
            _definition(retry_on_failure=False),
            tmp_path / "in.png",
            uuid4(),
            "a" * 32,
            _fingerprint(),
        )

        assert adapter.call_count == 1
        assert result.status == MediaRepresentationStatus.FAILED

    def test_adapter_exception_is_treated_as_failed_attempt_and_retried(self, tmp_path):
        adapter = FakeRaisingAdapter()
        orchestrator = PipelineExecutionOrchestrator()

        result = orchestrator.run(
            adapter,
            _definition(retry_on_failure=True),
            tmp_path / "in.png",
            uuid4(),
            "a" * 32,
            _fingerprint(),
        )

        assert adapter.call_count == 2
        assert result.status == MediaRepresentationStatus.FAILED

    def test_embedded_shell_text_in_output_is_never_executed(self, tmp_path, monkeypatch):
        """Even a maximally adversarial model output is just data.

        The orchestrator only ever reads `textual_payload` as text; it does
        not shell out based on representation content in any code path.
        """
        import subprocess

        def _forbidden(*args, **kwargs):
            raise AssertionError("orchestrator must never invoke subprocess directly")

        monkeypatch.setattr(subprocess, "run", _forbidden)
        monkeypatch.setattr(subprocess, "Popen", _forbidden)

        adapter = FakeAgentInjectionAttemptAdapter()
        orchestrator = PipelineExecutionOrchestrator()

        result = orchestrator.run(
            adapter, _definition(), tmp_path / "in.png", uuid4(), "a" * 32, _fingerprint()
        )

        assert result.status == MediaRepresentationStatus.CURRENT
        assert result.textual_payload == "'; rm -rf / #"


# ---------------------------------------------------------------------------
# JSON model-output validation helper
# ---------------------------------------------------------------------------


class TestValidateJsonOutput:
    def test_accepts_well_formed_payload(self):
        is_valid, error = validate_json_output(
            {"text": "hello", "confidence": 0.9},
            required_keys={"text"},
            expected_types={"text": str, "confidence": float},
        )
        assert is_valid is True
        assert error is None

    def test_rejects_non_dict_payload(self):
        is_valid, error = validate_json_output("not a dict", required_keys=set(), expected_types={})
        assert is_valid is False
        assert error is not None

    def test_rejects_missing_required_key(self):
        is_valid, error = validate_json_output({}, required_keys={"text"}, expected_types={})
        assert is_valid is False
        assert "text" in error

    def test_rejects_wrong_type(self):
        is_valid, error = validate_json_output(
            {"confidence": "not-a-number"},
            required_keys=set(),
            expected_types={"confidence": float},
        )
        assert is_valid is False
