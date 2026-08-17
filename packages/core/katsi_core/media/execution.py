"""Bounded subprocess execution and strict output validation for media pipelines.

Security invariants (see design.md Decision 4 and openspec change section 3):

- Every subprocess invocation uses `shell=False`. No string is ever passed
  to a shell interpreter.
- The executable and argument template come exclusively from an
  owner-registered `MediaPipelineDefinition`. Agents never supply an
  executable path, model identity, or command string; they only ever
  supply the small set of placeholder values (`input_path`, `output_path`,
  `working_directory`) that this module substitutes into the fixed
  template, and those substitutions are always paths computed by this
  module, never raw agent-authored strings.
- Environment variables are drawn from an explicit per-pipeline allowlist,
  never inherited wholesale from the host process.
- Every invocation runs inside a fresh, private temporary directory that is
  removed after execution.
- Output is bounded to `max_output_bytes`; a timeout of `timeout_seconds`
  is enforced and the process is killed on expiry.
- Network access is denied where the host platform supports isolating it;
  `network_isolation_applied` on the result reports whether isolation was
  actually enforced so callers never assume unavailable isolation happened.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from katsi_core.media.contracts import (
    ContentHash,
    DerivedRepresentation,
    MediaCoverage,
    MediaPipelineDefinition,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    ProducerProvenance,
    RepresentationError,
    ResourceVersionId,
)
from katsi_core.media.protocols import MediaPipelineProtocol

# Placeholders that may appear in a pipeline definition's fixed_args template
# (see MediaPipelineDefinition.fixed_args in contracts.py). These are the
# ONLY substitutions performed; anything else in the template is passed
# through literally as a fixed, owner-authored argument. This set is a
# security invariant, not owner-configurable: it is intentionally not a
# field on MediaPipelineDefinition, since making it configurable would let
# an owner definition request substitution of an arbitrary future value
# instead of the exact three paths this module computes itself.
ALLOWED_ARG_PLACEHOLDERS = frozenset({"input_path", "output_path", "working_directory"})

# Kinds that require a non-null textual_payload per DerivedRepresentation's
# own validation (see contracts.py DerivedRepresentation.validate_content).
# A FAILED representation must still satisfy this contract.
_TEXT_REPRESENTATION_KINDS = {
    MediaRepresentationKind.EXTRACTED_TEXT,
    MediaRepresentationKind.OCR_TEXT,
    MediaRepresentationKind.IMAGE_CAPTION,
    MediaRepresentationKind.TRANSCRIPT_SEGMENT,
}


class SubprocessSecurityError(Exception):
    """Raised when a pipeline definition or invocation violates security policy."""


class SubprocessTimeoutError(Exception):
    """Raised when a bounded subprocess exceeds its configured timeout."""


@dataclass(frozen=True)
class BoundedExecutionResult:
    """Result of a single bounded subprocess invocation."""

    exit_code: int
    timed_out: bool
    output_truncated: bool
    stdout_sample: str
    stderr_sample: str
    stdout_bytes: int
    stderr_bytes: int
    duration_seconds: float
    network_isolation_applied: bool


def _truncate(data: bytes, max_bytes: int) -> tuple[str, bool]:
    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace"), False
    truncated = data[:max_bytes]
    return truncated.decode("utf-8", errors="ignore"), True


def _prepare_environment(definition: MediaPipelineDefinition) -> dict[str, str]:
    """Build a sanitized environment containing only allowlisted variables."""
    env: dict[str, str] = {}
    for name in definition.allowed_env_vars:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value

    # Minimal safe defaults required for most executables to run at all.
    env["PATH"] = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["HOME"] = os.environ.get("HOME", str(Path.home()))
    return env


def _network_isolation_prefix(definition: MediaPipelineDefinition, sandbox_dir: Path) -> list[str]:
    """Return a command prefix that denies network access, if supported.

    Returns an empty list when the host platform has no supported isolation
    mechanism available; callers must treat that as "not applied" rather
    than silently assuming the child process cannot reach the network.
    """
    if not definition.network_disabled:
        return []

    system = platform.system()
    if system == "Linux" and shutil.which("unshare") is not None:
        return ["unshare", "--net", "--"]

    if system == "Darwin" and shutil.which("sandbox-exec") is not None:
        profile_path = sandbox_dir / "network-deny.sb"
        profile_path.write_text("(version 1)\n(allow default)\n(deny network*)\n", encoding="utf-8")
        return ["sandbox-exec", "-f", str(profile_path), "--"]

    return []


def _substitute_args(fixed_args: list[str], substitutions: dict[str, str]) -> list[str]:
    """Substitute only the allowed placeholders into a fixed argument template.

    Every element of `fixed_args` is owner-authored, fixed configuration.
    The only dynamic content permitted is replacement of `{placeholder}`
    tokens drawn from `ALLOWED_ARG_PLACEHOLDERS`, using values this module
    computed itself (never a raw agent-supplied string). `str.format` raises
    `KeyError` for any placeholder not present in `substitutions`, which is
    always built with exactly the keys in `ALLOWED_ARG_PLACEHOLDERS` -- so
    the allowlist is enforced structurally by what the caller supplies, not
    by a separate membership check here.
    """
    resolved: list[str] = []
    for arg in fixed_args:
        try:
            resolved.append(arg.format(**substitutions))
        except KeyError as e:
            raise SubprocessSecurityError(
                f"Argument template references unknown placeholder: {e}"
            ) from e
        except (IndexError, ValueError) as e:
            raise SubprocessSecurityError(f"Invalid argument template '{arg}': {e}") from e
    return resolved


class BoundedSubprocessExecutor:
    """Executes owner-registered media pipeline commands under strict bounds.

    This is the only component in the media pipeline that ever calls
    `subprocess`. It accepts a `MediaPipelineDefinition` -- never a raw
    command string -- and always runs with `shell=False`.
    """

    def execute(
        self,
        definition: MediaPipelineDefinition,
        input_path: Path,
        working_directory: Path,
        output_path: Path | None = None,
    ) -> BoundedExecutionResult:
        """Run the pipeline's fixed command against a single input file.

        Args:
            definition: Owner-registered pipeline definition. Its
                `executable_path` and `fixed_args` are the only source of
                the command; nothing here is agent-supplied.
            input_path: Path to the source file (already resolved by core,
                never an agent-supplied path string).
            working_directory: Private temporary directory for this
                invocation. Removed by the caller after use.
            output_path: Optional output path for pipelines that write a
                result file rather than emitting output on stdout.

        Returns:
            BoundedExecutionResult with bounded, truncated output.

        Raises:
            SubprocessSecurityError: If the definition is unsafe to execute
                (shell enabled, missing executable, or unresolved template).
        """
        if definition.shell_enabled:
            raise SubprocessSecurityError(
                f"Pipeline '{definition.id}' has shell_enabled=True; refusing to execute"
            )
        if not definition.executable_path:
            raise SubprocessSecurityError(
                f"Pipeline '{definition.id}' has no fixed executable_path; refusing to execute"
            )

        substitutions = {
            "input_path": str(input_path),
            "working_directory": str(working_directory),
            "output_path": str(output_path) if output_path is not None else "",
        }
        args = _substitute_args(definition.fixed_args, substitutions)
        network_prefix = _network_isolation_prefix(definition, working_directory)
        cmd = [*network_prefix, definition.executable_path, *args]

        env = _prepare_environment(definition)
        started = datetime.now(UTC)

        stdout_bytes = b""
        stderr_bytes = b""
        exit_code = -1
        timed_out = False

        process: subprocess.Popen[bytes] | None = None
        timer: threading.Timer | None = None
        try:
            process = subprocess.Popen(  # noqa: S603 -- shell=False, fixed argv only
                cmd,
                cwd=working_directory,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                shell=False,
            )
            timer = threading.Timer(definition.timeout_seconds, process.kill)
            timer.start()
            try:
                stdout_bytes, stderr_bytes = process.communicate(timeout=definition.timeout_seconds)
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                stdout_bytes, stderr_bytes = process.communicate()
                exit_code = -1
        finally:
            if timer is not None:
                timer.cancel()
            if process is not None and process.poll() is None:
                process.kill()

        duration = (datetime.now(UTC) - started).total_seconds()
        stdout_sample, stdout_truncated = _truncate(stdout_bytes, definition.max_output_bytes)
        stderr_sample, stderr_truncated = _truncate(stderr_bytes, definition.max_output_bytes)

        return BoundedExecutionResult(
            exit_code=exit_code,
            timed_out=timed_out,
            output_truncated=stdout_truncated or stderr_truncated,
            stdout_sample=stdout_sample,
            stderr_sample=stderr_sample,
            stdout_bytes=len(stdout_bytes),
            stderr_bytes=len(stderr_bytes),
            duration_seconds=duration,
            network_isolation_applied=bool(network_prefix),
        )


def _failed_representation(
    resource_version_id: ResourceVersionId,
    source_content_hash: ContentHash,
    pipeline_fingerprint: PipelineFingerprint,
    definition: MediaPipelineDefinition,
    error_message: str,
    attempts: int,
) -> DerivedRepresentation:
    now = datetime.now(UTC)
    kind = pipeline_fingerprint.representation_kind

    # DerivedRepresentation enforces content invariants regardless of
    # status, so a FAILED representation must still satisfy them.
    textual_payload: str | None = None
    blob_reference: str | None = None
    blob_hash: str | None = None
    blob_byte_count: int | None = None
    if kind in _TEXT_REPRESENTATION_KINDS:
        textual_payload = ""
    elif kind in {
        MediaRepresentationKind.THUMBNAIL,
        MediaRepresentationKind.KEYFRAME,
        MediaRepresentationKind.PROXY_MEDIA,
    }:
        blob_reference = "unavailable"
        blob_hash = "0" * 32
        blob_byte_count = 0

    return DerivedRepresentation(
        id=uuid4(),
        resource_version_id=resource_version_id,
        kind=kind,
        media_type="application/octet-stream",
        status=MediaRepresentationStatus.FAILED,
        created_at=now,
        updated_at=now,
        textual_payload=textual_payload,
        blob_reference=blob_reference,
        blob_hash=blob_hash,
        blob_byte_count=blob_byte_count,
        coverage=MediaCoverage(is_complete=False, coverage_fraction=0.0, detail="Pipeline failed"),
        producer=ProducerProvenance(
            producer_type=definition.producer_type,
            adapter_name=definition.id,
            adapter_version="unknown",
            model_identity=definition.model_identity,
        ),
        pipeline_fingerprint=pipeline_fingerprint,
        error=RepresentationError(
            error_category="invalid_output" if attempts > 1 else "processing_error",
            error_message=error_message,
            is_retriable=False,
            diagnostic_info={"attempts": str(attempts), "pipeline_id": definition.id},
        ),
    )


class PipelineExecutionOrchestrator:
    """Orchestrates pipeline invocation, strict output validation, and retry.

    Model-backed and deterministic outputs alike are validated against the
    pipeline's declared output contract. An invalid or errored attempt is
    retried at most once (when `definition.retry_on_failure` is true); a
    second invalid result produces a FAILED representation rather than
    propagating a raw exception or accepting unvalidated output.
    """

    def run(
        self,
        adapter: MediaPipelineProtocol,
        definition: MediaPipelineDefinition,
        file_path: Path,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        pipeline_fingerprint: PipelineFingerprint,
    ) -> DerivedRepresentation:
        """Run `adapter.process` with strict validation and one retry.

        Each attempt gets its own fresh, private temporary working
        directory that is removed once the attempt completes.
        """
        max_attempts = 2 if definition.retry_on_failure else 1
        last_error = "Pipeline produced no output"

        for _attempt in range(1, max_attempts + 1):
            with tempfile.TemporaryDirectory(prefix="katsi-media-") as tmp:
                working_directory = Path(tmp)
                try:
                    representation = adapter.process(
                        file_path,
                        resource_version_id,
                        source_content_hash,
                        pipeline_fingerprint,
                        working_directory,
                    )
                except Exception as e:  # noqa: BLE001 -- any adapter failure is a retry candidate
                    last_error = f"Pipeline raised an exception: {e}"
                    continue

                if not definition.strict_output_contract:
                    return representation

                is_valid, validation_error = adapter.validate_output(
                    representation, pipeline_fingerprint.representation_kind
                )
                if is_valid:
                    return representation

                last_error = validation_error or "Pipeline output failed validation"

        return _failed_representation(
            resource_version_id,
            source_content_hash,
            pipeline_fingerprint,
            definition,
            last_error,
            attempts=max_attempts,
        )


def validate_json_output(
    output: Any, required_keys: set[str], expected_types: dict[str, type]
) -> tuple[bool, str | None]:
    """Strictly validate a model-backed JSON output payload.

    Rejects anything that is not a `dict`, has missing required keys, or
    has a value of the wrong type for a checked key. Used by model-backed
    pipeline adapters to implement `validate_output`.
    """
    if not isinstance(output, dict):
        return False, f"Expected a JSON object, got {type(output).__name__}"

    missing = required_keys - output.keys()
    if missing:
        return False, f"Missing required keys: {sorted(missing)}"

    for key, expected_type in expected_types.items():
        if key in output and not isinstance(output[key], expected_type):
            return False, (
                f"Key '{key}' has wrong type: expected {expected_type.__name__}, "
                f"got {type(output[key]).__name__}"
            )

    return True, None
