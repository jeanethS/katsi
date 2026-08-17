"""Safe verifier execution with bounded output and secret redaction."""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from katsi_core.workspace.rollback import Preimage
from katsi_core.workspace.verification import (
    VerifierDefinition,
    VerifierExecution,
)


class SecretPattern(StrEnum):
    """Known secret patterns to redact from verifier output."""

    API_KEY = r"(?i)((api[_-]?key|apikey)[\s:=]+)\S{8,}"
    TOKEN = r"(?i)((token|auth[_-]?token)[\s:=]+)\S{8,}"
    PASSWORD = r"(?i)((password|passwd|pwd)[\s:=]+)\S{8,}"
    SECRET = r"(?i)((secret|private[_-]?key)[\s:=]+)\S{8,}"
    BEARER = r"(?i)(bearer[\s:]+)\S{8,}"


class VerifierExecutionError(Exception):
    """Base class for verifier execution errors."""

    def __init__(self, message: str, exit_code: int | None = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class VerifierTimeoutError(VerifierExecutionError):
    """Verifier exceeded configured timeout."""


class VerifierSecurityError(VerifierExecutionError):
    """Verifier violated security constraints."""


def redact_secrets(text: str, patterns: tuple[SecretPattern, ...] = ()) -> str:
    """Redact detected secrets from verifier output."""
    import re

    redacted = text
    all_patterns = list(patterns) + list(SecretPattern)

    for pattern in all_patterns:
        # Use capturing group to preserve the prefix and only replace the value
        redacted = re.sub(pattern.value, r"\1***REDACTED***", redacted, flags=re.IGNORECASE)

    return redacted


def truncate_output(text: str, max_bytes: int) -> tuple[str, bool]:
    """Truncate output to byte limit, preserving character boundaries."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text, False

    # Truncate at character boundary
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, True


class VerifierExecutor:
    """Safe verifier execution with bounded output and no held transactions."""

    def __init__(self, workspace_root: Path, quarantine_dir: Path) -> None:
        self._workspace_root = workspace_root
        self._quarantine_dir = quarantine_dir
        self._quarantine_dir.mkdir(parents=True, exist_ok=True)

        # Track running processes for timeout enforcement
        self._running_processes: dict[UUID, subprocess.Popen[bytes]] = {}
        self._timeout_timers: dict[UUID, threading.Timer] = {}

    def execute(
        self,
        verifier: VerifierDefinition,
        change_set_id: UUID,
        variable_args: dict[str, str],
        stdin_data: str | None = None,
        cwd_path: Path | None = None,
    ) -> VerifierExecution:
        """Execute a verifier safely with no held transaction.

        Args:
            verifier: The verifier definition to execute
            change_set_id: The Change Set being verified
            variable_args: Variable argument substitutions
            stdin_data: Optional data to provide on stdin
            cwd_path: Working directory (defaults to workspace root or verifier scope)

        Returns:
            VerifierExecution with bounded, redacted output

        Raises:
            VerifierSecurityError: If verifier configuration is unsafe
            VerifierTimeoutError: If verifier exceeds timeout
        """
        execution_id = uuid4()
        started_at = datetime.now(UTC)

        # Validate security constraints
        self._validate_verifier_security(verifier)

        # Build command with argument prefix and variable substitutions
        cmd = self._build_command(verifier, variable_args)

        # Determine working directory
        work_dir = self._resolve_working_dir(verifier, cwd_path)

        # Prepare environment (allowlist only)
        env = self._prepare_environment(verifier)

        # Execute without holding any database transaction
        try:
            result = self._execute_subprocess(
                execution_id,
                cmd,
                verifier,
                work_dir,
                env,
                stdin_data,
            )
        except Exception as e:
            # Ensure no process is left running
            self._cleanup_execution(execution_id)
            raise VerifierExecutionError(f"Verifier execution failed: {e}") from e

        duration = (datetime.now(UTC) - started_at).total_seconds()

        # Prepare output samples with redaction and truncation
        stdout_sample, stdout_truncated = truncate_output(
            result["stdout"], verifier.max_output_bytes
        )
        stderr_sample, stderr_truncated = truncate_output(
            result["stderr"], verifier.max_output_bytes
        )

        # Redact secrets from samples
        stdout_sample = redact_secrets(stdout_sample)
        stderr_sample = redact_secrets(stderr_sample)

        return VerifierExecution(
            verifier_id=verifier.id,
            change_set_id=change_set_id,
            exit_code=result["exit_code"],
            signal=result["signal"],
            timed_out=result["timed_out"],
            output_truncated=stdout_truncated or stderr_truncated,
            stdout_bytes=len(result["stdout"].encode("utf-8")),
            stderr_bytes=len(result["stderr"].encode("utf-8")),
            stdout_sample=stdout_sample,
            stderr_sample=stderr_sample,
            duration_seconds=duration,
            occurred_at=started_at.isoformat(),
        )

    def _validate_verifier_security(self, verifier: VerifierDefinition) -> None:
        """Validate verifier configuration for security constraints."""
        # Check executable exists and is within allowed bounds
        exe_path = Path(verifier.executable_path)
        if not exe_path.is_absolute():
            exe_path = (self._workspace_root / verifier.executable_path).resolve()

        if not exe_path.exists():
            raise VerifierSecurityError(
                f"Verifier executable not found: {verifier.executable_path}"
            )

        # Ensure no path traversal in working directory
        if verifier.working_directory_scope:
            scope = Path(verifier.working_directory_scope)
            if ".." in scope.parts or not scope.is_absolute():
                raise VerifierSecurityError(
                    f"Invalid working directory scope: {verifier.working_directory_scope}"
                )

    def _build_command(
        self, verifier: VerifierDefinition, variable_args: dict[str, str]
    ) -> list[str]:
        """Build the command list with fixed prefix and variable substitutions."""
        cmd = [verifier.executable_path]

        # Add fixed argument prefix
        cmd.extend(verifier.argument_prefix)

        # Add variable arguments in the order specified by names
        for arg_name in verifier.variable_arg_names:
            if arg_name in variable_args:
                cmd.append(str(variable_args[arg_name]))
            else:
                raise VerifierSecurityError(f"Missing required variable argument: {arg_name}")

        return cmd

    def _resolve_working_dir(self, verifier: VerifierDefinition, cwd_path: Path | None) -> Path:
        """Resolve the working directory for verifier execution."""
        if cwd_path is not None:
            return cwd_path

        if verifier.working_directory_scope:
            scope = Path(verifier.working_directory_scope)
            if scope.is_absolute():
                return scope
            return (self._workspace_root / scope).resolve()

        return self._workspace_root

    def _prepare_environment(self, verifier: VerifierDefinition) -> dict[str, str]:
        """Prepare environment with only allowed variables."""
        env = {}

        # Copy only allowlisted variables from current environment
        for var_name in verifier.environment_allowlist:
            value = os.environ.get(var_name)
            if value is not None:
                env[var_name] = value

        # Always set minimal safe environment
        env["PATH"] = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        env["HOME"] = os.environ.get("HOME", str(Path.home()))

        return env

    def _execute_subprocess(
        self,
        execution_id: UUID,
        cmd: list[str],
        verifier: VerifierDefinition,
        work_dir: Path,
        env: dict[str, str],
        stdin_data: str | None,
    ) -> dict[str, object]:
        """Execute subprocess with timeout and output limits, no transaction held."""
        stdout_bytes = b""
        stderr_bytes = b""
        exit_code = -1
        signal_num: int | None = None
        timed_out = False

        try:
            # Use shell=False for security
            process = subprocess.Popen(
                cmd,
                cwd=work_dir,
                env=env,
                stdin=subprocess.PIPE if stdin_data else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,  # CRITICAL: No shell interpretation
            )

            self._running_processes[execution_id] = process

            # Set timeout timer
            timer = threading.Timer(
                verifier.timeout_seconds,
                self._timeout_handler,
                args=[execution_id],
            )
            self._timeout_timers[execution_id] = timer
            timer.start()

            # Communicate with byte limits
            stdin_input = stdin_data.encode("utf-8") if stdin_data else None

            try:
                stdout_bytes, stderr_bytes = process.communicate(
                    input=stdin_input,
                    timeout=verifier.timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                stdout_bytes, stderr_bytes = process.communicate()
                exit_code = -1
            finally:
                timer.cancel()

            exit_code = process.returncode

        except Exception as e:
            # Handle any execution errors
            stderr_bytes = str(e).encode("utf-8")
            exit_code = -1

        finally:
            self._cleanup_execution(execution_id)

        return {
            "exit_code": exit_code,
            "signal": signal_num,
            "timed_out": timed_out,
            "stdout": stdout_bytes.decode("utf-8", errors="replace"),
            "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        }

    def _timeout_handler(self, execution_id: UUID) -> None:
        """Handle verifier timeout by killing the process."""
        process = self._running_processes.get(execution_id)
        if process:
            with contextlib.suppress(Exception):
                process.kill()

    def _cleanup_execution(self, execution_id: UUID) -> None:
        """Clean up tracking for an execution."""
        self._running_processes.pop(execution_id, None)
        timer = self._timeout_timers.pop(execution_id, None)
        if timer:
            timer.cancel()

    def create_preimage(
        self,
        original_path: Path,
        change_set_id: UUID,
        operation_ordinal: int,
        expires_in_hours: int | None = None,
    ) -> Preimage:
        """Create a recoverable preimage before mutation."""
        if not original_path.exists():
            raise FileNotFoundError(f"Cannot create preimage: {original_path} does not exist")

        import blake3

        # Calculate content hash
        content_hash = blake3.blake3(original_path.read_bytes()).hexdigest()
        byte_count = original_path.stat().st_size

        # Create quarantine path
        preimage_id = uuid4()
        quarantine_path = self._quarantine_dir / f"{preimage_id.hex}.preimage"

        # Copy file to quarantine
        import shutil

        shutil.copy2(original_path, quarantine_path)

        # Set expiration
        from datetime import timedelta

        expires_at = None
        if expires_in_hours:
            expires_at = datetime.now(UTC) + timedelta(hours=expires_in_hours)

        return Preimage(
            id=preimage_id,
            change_set_id=change_set_id,
            operation_ordinal=operation_ordinal,
            original_path=str(original_path),
            preimage_path=str(quarantine_path),
            content_hash=content_hash,
            byte_count=byte_count,
            quarantined=True,
            quarantine_path=str(quarantine_path),
            created_at=datetime.now(UTC),
            expires_at=expires_at,
        )
