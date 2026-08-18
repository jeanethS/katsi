"""Detector and media-pipeline protocols with lazy adapter loading.

This module defines the abstract protocols that media detectors and pipelines
must implement. It supports lazy loading of optional adapters and provides
availability probing for hardware/software dependencies.

Key features:
- Abstract protocols for media detection and processing
- Lazy loading of optional adapter dependencies
- Availability probing for hardware/software requirements
- Plugin-style architecture for extensible media support
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from katsi_core.media.contracts import (
        ContentHash,
        DerivedRepresentation,
        MediaDescriptor,
        MediaPipelineDefinition,
        MediaRepresentationKind,
        PipelineFingerprint,
        ResourceVersionId,
    )


class AdapterLoadError(Exception):
    """Exception raised when an adapter fails to load."""

    def __init__(self, adapter_name: str, reason: str) -> None:
        """Initialize adapter load error.

        Args:
            adapter_name: Name of the adapter that failed to load
            reason: Human-readable reason for failure
        """
        self.adapter_name = adapter_name
        self.reason = reason
        super().__init__(f"Failed to load adapter '{adapter_name}': {reason}")


class HardwareRequirement(StrEnum):
    """Hardware requirements for media adapters."""

    NONE = "none"
    CPU_ONLY = "cpu_only"
    GPU_CUDA = "gpu_cuda"
    GPU_METAL = "gpu_metal"
    GPU_ROCM = "gpu_rocm"
    GPU_ANY = "gpu_any"
    TPU = "tpu"
    NPU = "npu"


class SoftwareDependency(StrEnum):
    """Software dependencies for media adapters."""

    NONE = "none"
    FFMPEG = "ffmpeg"
    IMAGE_MAGICK = "image_magick"
    POPPLER = "poppler"
    TESSERACT = "tesseract"
    PYTHON_OPENCV = "python_opencv"
    PYTHON_PILLOW = "python_pillow"
    PYTHON_PYDUB = "python_pydub"
    PYTHON_TRANSFORMERS = "python_transformers"
    PYTHON_TORCH = "python_torch"
    PYTHON_TORCH_VISION = "python_torch_vision"
    PYTHON_NUMPY = "python_numpy"
    VENV_BINARIES = "venv_binaries"


class MediaDetectorProtocol(ABC):
    """Abstract protocol for media detector adapters.

    Media detectors inspect file content to determine media type, dimensions,
    duration, and other structural metadata without executing embedded content.
    They use file signatures and container metadata rather than relying on
    file extensions alone.

    Implementations must be deterministic and should not execute embedded content.
    """

    @classmethod
    @abstractmethod
    def get_adapter_name(cls) -> str:
        """Get the unique adapter name.

        Returns:
            Unique adapter identifier
        """
        pass

    @classmethod
    @abstractmethod
    def get_adapter_version(cls) -> str:
        """Get the adapter version string.

        Returns:
            Version string (e.g., "1.0.0")
        """
        pass

    @classmethod
    @abstractmethod
    def get_supported_mime_patterns(cls) -> list[str]:
        """Get list of supported MIME type patterns.

        Returns:
            List of glob patterns for MIME types
        """
        pass

    @classmethod
    @abstractmethod
    def get_hardware_requirements(cls) -> list[HardwareRequirement]:
        """Get hardware requirements for this detector.

        Returns:
            List of required hardware features
        """
        pass

    @classmethod
    @abstractmethod
    def get_software_dependencies(cls) -> list[SoftwareDependency]:
        """Get software dependencies for this detector.

        Returns:
            List of required software dependencies
        """
        pass

    @classmethod
    def check_availability(cls) -> tuple[bool, str | None]:
        """Check if this detector is available and ready to use.

        This method should verify hardware and software dependencies without
        actually loading heavy dependencies into memory.

        Returns:
            Tuple of (is_available, error_message)
        """
        # Check software dependencies
        for dependency in cls.get_software_dependencies():
            available, error = cls._check_software_dependency(dependency)
            if not available:
                return False, f"Software dependency '{dependency.value}' not available: {error}"

        # Check hardware requirements
        for requirement in cls.get_hardware_requirements():
            available, error = cls._check_hardware_requirement(requirement)
            if not available:
                return False, f"Hardware requirement '{requirement.value}' not met: {error}"

        return True, None

    @classmethod
    def _check_software_dependency(cls, dependency: SoftwareDependency) -> tuple[bool, str | None]:
        """Check if a software dependency is available.

        Args:
            dependency: Software dependency to check

        Returns:
            Tuple of (is_available, error_message)
        """
        if dependency == SoftwareDependency.NONE:
            return True, None

        elif dependency == SoftwareDependency.FFMPEG:
            return cls._check_executable("ffmpeg")

        elif dependency == SoftwareDependency.IMAGE_MAGICK:
            return cls._check_executable("convert")  # ImageMagick's primary tool

        elif dependency == SoftwareDependency.POPPLER:
            return cls._check_executable("pdftoppm")

        elif dependency == SoftwareDependency.TESSERACT:
            return cls._check_executable("tesseract")

        elif dependency == SoftwareDependency.PYTHON_OPENCV:
            return cls._check_python_module("cv2")

        elif dependency == SoftwareDependency.PYTHON_PILLOW:
            return cls._check_python_module("PIL")

        elif dependency == SoftwareDependency.PYTHON_PYDUB:
            return cls._check_python_module("pydub")

        elif dependency == SoftwareDependency.PYTHON_TRANSFORMERS:
            return cls._check_python_module("transformers")

        elif dependency == SoftwareDependency.PYTHON_TORCH:
            return cls._check_python_module("torch")

        elif dependency == SoftwareDependency.PYTHON_TORCH_VISION:
            return cls._check_python_module("torchvision")

        elif dependency == SoftwareDependency.PYTHON_NUMPY:
            return cls._check_python_module("numpy")

        elif dependency == SoftwareDependency.VENV_BINARIES:
            # Check if we're in a virtual environment with accessible binaries
            return cls._check_venv_binaries()

        else:
            return False, f"Unknown software dependency: {dependency.value}"

    @classmethod
    def _check_hardware_requirement(
        cls, requirement: HardwareRequirement
    ) -> tuple[bool, str | None]:
        """Check if a hardware requirement is met.

        Args:
            requirement: Hardware requirement to check

        Returns:
            Tuple of (is_available, error_message)
        """
        if requirement == HardwareRequirement.NONE:
            return True, None

        elif requirement == HardwareRequirement.CPU_ONLY:
            # CPU is always available
            return True, None

        elif requirement == HardwareRequirement.GPU_CUDA:
            # Check for CUDA via PyTorch if available
            try:
                import torch

                if torch.cuda.is_available():
                    return True, None
                else:
                    return False, "CUDA device not available"
            except ImportError:
                return False, "PyTorch not available for CUDA detection"

        elif requirement == HardwareRequirement.GPU_METAL:
            # Check for Metal support (macOS)
            if os.uname().sysname != "Darwin":
                return False, "Metal is only available on macOS"
            try:
                import torch

                if torch.backends.mps.is_available():
                    return True, None
                else:
                    return False, "Metal device not available"
            except ImportError:
                return False, "PyTorch not available for Metal detection"

        elif requirement == HardwareRequirement.GPU_ROCM:
            # Check for ROCm via PyTorch if available
            try:
                import torch

                if torch.cuda.is_available() and "rocm" in str(torch.version.cuda).lower():
                    return True, None
                else:
                    return False, "ROCm device not available"
            except ImportError:
                return False, "PyTorch not available for ROCm detection"

        elif requirement == HardwareRequirement.GPU_ANY:
            # Check for any GPU support
            cuda_available, _ = cls._check_hardware_requirement(HardwareRequirement.GPU_CUDA)
            if cuda_available:
                return True, None

            metal_available, _ = cls._check_hardware_requirement(HardwareRequirement.GPU_METAL)
            if metal_available:
                return True, None

            rocm_available, _ = cls._check_hardware_requirement(HardwareRequirement.GPU_ROCM)
            if rocm_available:
                return True, None

            return False, "No GPU support detected (CUDA, Metal, or ROCm)"

        elif requirement == HardwareRequirement.TPU:
            # Check for TPU support
            try:
                import torch

                # Note: TPU detection would require torch_xla
                return False, "TPU support not yet implemented"
            except ImportError:
                return False, "PyTorch not available for TPU detection"

        elif requirement == HardwareRequirement.NPU:
            # Check for NPU support
            return False, "NPU support not yet implemented"

        else:
            return False, f"Unknown hardware requirement: {requirement.value}"

    @classmethod
    def _check_executable(cls, executable_name: str) -> tuple[bool, str | None]:
        """Check if an executable is available in PATH.

        Args:
            executable_name: Name of executable to check

        Returns:
            Tuple of (is_available, error_message)
        """
        path = shutil.which(executable_name)
        if path is None:
            return False, f"Executable '{executable_name}' not found in PATH"

        # Try to execute with version flag to verify it works
        try:
            result = subprocess.run(
                [executable_name, "--version"],
                capture_output=True,
                timeout=5,
                text=True,
            )
            if result.returncode == 0:
                return True, None
            else:
                # Some tools use -v instead of --version
                result = subprocess.run(
                    [executable_name, "-v"],
                    capture_output=True,
                    timeout=5,
                    text=True,
                )
                if result.returncode == 0:
                    return True, None
                return False, f"Executable '{executable_name}' failed version check"
        except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError) as e:
            return False, f"Executable '{executable_name}' check failed: {e}"

    @classmethod
    def _check_python_module(cls, module_name: str) -> tuple[bool, str | None]:
        """Check if a Python module is available.

        Args:
            module_name: Name of Python module to check

        Returns:
            Tuple of (is_available, error_message)
        """
        try:
            importlib.import_module(module_name)
            return True, None
        except ImportError as e:
            return False, f"Python module '{module_name}' not available: {e}"

    @classmethod
    def _check_venv_binaries(cls) -> tuple[bool, str | None]:
        """Check if virtual environment binaries are accessible.

        Returns:
            Tuple of (is_available, error_message)
        """
        # Check if we're in a virtual environment
        if os.getenv("VIRTUAL_ENV") is None:
            return False, "Not in a virtual environment"

        # Check if bin directory exists and is accessible
        venv_path = Path(os.getenv("VIRTUAL_ENV"))
        bin_dir = venv_path / "bin"
        if not bin_dir.exists():
            return False, f"Virtual environment bin directory not found: {bin_dir}"

        return True, None

    @abstractmethod
    def detect_media(self, file_path: Path, content_hash: ContentHash) -> MediaDescriptor:
        """Detect media type and extract metadata from file.

        This method should inspect file signatures and container metadata
        without executing embedded content.

        Args:
            file_path: Path to the file to inspect
            content_hash: Hash of the file content

        Returns:
            MediaDescriptor with detected information

        Raises:
            AdapterLoadError: If detection fails
        """
        pass

    @abstractmethod
    def validate_file_integrity(
        self, file_path: Path, content_hash: ContentHash
    ) -> tuple[bool, str | None]:
        """Validate file integrity using content hash.

        Args:
            file_path: Path to the file to validate
            content_hash: Expected content hash

        Returns:
            Tuple of (is_valid, error_message)
        """
        pass


class MediaPipelineProtocol(ABC):
    """Abstract protocol for media processing pipelines.

    Media pipelines transform source content into derived representations
    through deterministic processing or model-backed analysis. Each pipeline
    declares its inputs, outputs, resource budgets, and execution policies.

    Pipelines must never accept agent-supplied commands and must enforce
    strict resource limits and output validation.
    """

    @classmethod
    @abstractmethod
    def get_adapter_name(cls) -> str:
        """Get the unique adapter name.

        Returns:
            Unique adapter identifier
        """
        pass

    @classmethod
    @abstractmethod
    def get_adapter_version(cls) -> str:
        """Get the adapter version string.

        Returns:
            Version string (e.g., "1.0.0")
        """
        pass

    @classmethod
    @abstractmethod
    def get_pipeline_definition(cls) -> MediaPipelineDefinition:
        """Get the pipeline definition.

        Returns:
            Complete pipeline definition with all constraints
        """
        pass

    @classmethod
    def get_pipeline_id(cls) -> str:
        """Get the pipeline identifier.

        Returns:
            Pipeline ID from definition
        """
        return cls.get_pipeline_definition().id

    @classmethod
    @abstractmethod
    def get_hardware_requirements(cls) -> list[HardwareRequirement]:
        """Get hardware requirements for this pipeline.

        Returns:
            List of required hardware features
        """
        pass

    @classmethod
    @abstractmethod
    def get_software_dependencies(cls) -> list[SoftwareDependency]:
        """Get software dependencies for this pipeline.

        Returns:
            List of required software dependencies
        """
        pass

    @classmethod
    def check_availability(cls) -> tuple[bool, str | None]:
        """Check if this pipeline is available and ready to use.

        This method should verify hardware and software dependencies without
        actually loading heavy models into memory.

        Returns:
            Tuple of (is_available, error_message)
        """
        definition = cls.get_pipeline_definition()

        # Check software dependencies
        for dependency in cls.get_software_dependencies():
            available, error = cls._check_software_dependency(dependency)
            if not available:
                return False, f"Software dependency '{dependency.value}' not available: {error}"

        # Check hardware requirements
        for requirement in cls.get_hardware_requirements():
            available, error = cls._check_hardware_requirement(requirement)
            if not available:
                return False, f"Hardware requirement '{requirement.value}' not met: {error}"

        # Check availability probe if defined
        if definition.availability_probe:
            available, error = cls._check_availability_probe(definition.availability_probe)
            if not available:
                return False, f"Availability probe failed: {error}"

        return True, None

    @classmethod
    def _check_software_dependency(cls, dependency: SoftwareDependency) -> tuple[bool, str | None]:
        """Check if a software dependency is available.

        Args:
            dependency: Software dependency to check

        Returns:
            Tuple of (is_available, error_message)
        """
        # Use the same implementation as detector
        return MediaDetectorProtocol._check_software_dependency(dependency)

    @classmethod
    def _check_hardware_requirement(
        cls, requirement: HardwareRequirement
    ) -> tuple[bool, str | None]:
        """Check if a hardware requirement is met.

        Args:
            requirement: Hardware requirement to check

        Returns:
            Tuple of (is_available, error_message)
        """
        # Use the same implementation as detector
        return MediaDetectorProtocol._check_hardware_requirement(requirement)

    @classmethod
    def _check_availability_probe(cls, probe_command: str) -> tuple[bool, str | None]:
        """Check if availability probe command succeeds.

        Args:
            probe_command: Command to execute for availability check

        Returns:
            Tuple of (is_available, error_message)
        """
        try:
            # Split command safely (no shell)
            parts = probe_command.split()
            result = subprocess.run(
                parts,
                capture_output=True,
                timeout=10,
                text=True,
            )
            if result.returncode == 0:
                return True, None
            else:
                return (
                    False,
                    f"Probe command failed with exit code {result.returncode}: {result.stderr}",
                )
        except subprocess.TimeoutExpired:
            return False, "Probe command timed out"
        except (FileNotFoundError, PermissionError) as e:
            return False, f"Probe command failed: {e}"

    @abstractmethod
    def process(
        self,
        file_path: Path,
        resource_version_id: ResourceVersionId,
        source_content_hash: ContentHash,
        pipeline_fingerprint: PipelineFingerprint,
        working_directory: Path,
    ) -> DerivedRepresentation:
        """Process media file and generate derived representation.

        This method must enforce all resource budgets and output validation
        as specified in the pipeline definition.

        Args:
            file_path: Path to source file
            resource_version_id: Source resource version ID
            source_content_hash: Hash of source content
            pipeline_fingerprint: Complete pipeline fingerprint
            working_directory: Private working directory for processing

        Returns:
            Derived representation

        Raises:
            AdapterLoadError: If processing fails
        """
        pass

    @abstractmethod
    def validate_output(
        self,
        output: Any,
        representation_kind: MediaRepresentationKind,
    ) -> tuple[bool, str | None]:
        """Validate pipeline output against contract.

        This method should enforce strict output validation and reject
        malformed or unexpected results.

        Args:
            output: Pipeline output to validate
            representation_kind: Expected representation kind

        Returns:
            Tuple of (is_valid, error_message)
        """
        pass


class LazyAdapterLoader:
    """Lazy loader for media adapters.

    This class provides lazy loading of optional adapter dependencies,
    allowing core to import media protocols without requiring heavy
    dependencies at module import time.
    """

    def __init__(self) -> None:
        """Initialize the lazy adapter loader."""
        self._loaded_detectors: dict[str, type[MediaDetectorProtocol]] = {}
        self._loaded_pipelines: dict[str, type[MediaPipelineProtocol]] = {}
        self._failed_loads: dict[str, AdapterLoadError] = {}

    def load_detector(self, detector_class_path: str) -> type[MediaDetectorProtocol] | None:
        """Load a detector class by module path.

        Args:
            detector_class_path: Full path to detector class (e.g., "package.module.ClassName")

        Returns:
            Loaded detector class, or None if loading failed

        Raises:
            AdapterLoadError: If loading fails
        """
        if detector_class_path in self._loaded_detectors:
            return self._loaded_detectors[detector_class_path]

        if detector_class_path in self._failed_loads:
            return None

        try:
            module_path, class_name = detector_class_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            detector_class = getattr(module, class_name)

            # Verify it's a valid detector protocol
            if not issubclass(detector_class, MediaDetectorProtocol):
                raise AdapterLoadError(
                    detector_class_path,
                    f"Class {class_name} does not implement MediaDetectorProtocol",
                )

            self._loaded_detectors[detector_class_path] = detector_class
            return detector_class

        except (ImportError, AttributeError, ValueError) as e:
            error = AdapterLoadError(detector_class_path, str(e))
            self._failed_loads[detector_class_path] = error
            return None

    def load_pipeline(self, pipeline_class_path: str) -> type[MediaPipelineProtocol] | None:
        """Load a pipeline class by module path.

        Args:
            pipeline_class_path: Full path to pipeline class (e.g., "package.module.ClassName")

        Returns:
            Loaded pipeline class, or None if loading failed

        Raises:
            AdapterLoadError: If loading fails
        """
        if pipeline_class_path in self._loaded_pipelines:
            return self._loaded_pipelines[pipeline_class_path]

        if pipeline_class_path in self._failed_loads:
            return None

        try:
            module_path, class_name = pipeline_class_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            pipeline_class = getattr(module, class_name)

            # Verify it's a valid pipeline protocol
            if not issubclass(pipeline_class, MediaPipelineProtocol):
                raise AdapterLoadError(
                    pipeline_class_path,
                    f"Class {class_name} does not implement MediaPipelineProtocol",
                )

            self._loaded_pipelines[pipeline_class_path] = pipeline_class
            return pipeline_class

        except (ImportError, AttributeError, ValueError) as e:
            error = AdapterLoadError(pipeline_class_path, str(e))
            self._failed_loads[pipeline_class_path] = error
            return None

    def get_available_detectors(
        self, detector_class_paths: list[str]
    ) -> list[type[MediaDetectorProtocol]]:
        """Get all available detector classes from a list.

        Args:
            detector_class_paths: List of detector class paths to load

        Returns:
            List of successfully loaded detector classes
        """
        available_detectors: list[type[MediaDetectorProtocol]] = []

        for detector_path in detector_class_paths:
            detector_class = self.load_detector(detector_path)
            if detector_class is not None:
                # Check availability
                is_available, _ = detector_class.check_availability()
                if is_available:
                    available_detectors.append(detector_class)

        return available_detectors

    def get_available_pipelines(
        self, pipeline_class_paths: list[str]
    ) -> list[type[MediaPipelineProtocol]]:
        """Get all available pipeline classes from a list.

        Args:
            pipeline_class_paths: List of pipeline class paths to load

        Returns:
            List of successfully loaded pipeline classes
        """
        available_pipelines: list[type[MediaPipelineProtocol]] = []

        for pipeline_path in pipeline_class_paths:
            pipeline_class = self.load_pipeline(pipeline_path)
            if pipeline_class is not None:
                # Check availability
                is_available, _ = pipeline_class.check_availability()
                if is_available:
                    available_pipelines.append(pipeline_class)

        return available_pipelines

    def clear_cache(self) -> None:
        """Clear the cache of loaded and failed adapters."""
        self._loaded_detectors.clear()
        self._loaded_pipelines.clear()
        self._failed_loads.clear()


# Global lazy loader instance
_lazy_loader = LazyAdapterLoader()


def get_lazy_loader() -> LazyAdapterLoader:
    """Get the global lazy adapter loader instance.

    Returns:
        Global LazyAdapterLoader instance
    """
    return _lazy_loader
