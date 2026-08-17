"""Adjacent same-filesystem staging with atomic replacement operations."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from katsi_core.config import ObserverSettings


class AdjacentStagingManager:
    """Same-filesystem staging with configured reserved names and atomic replacement."""

    def __init__(self, settings: ObserverSettings) -> None:
        self._settings = settings
        self._reserved_prefix = settings.reserved_path_prefix

    def get_stage_path(self, target_path: Path) -> Path:
        """Generate a staging path adjacent to the target file."""
        if not target_path.is_absolute():
            raise ValueError("Target path must be absolute")

        # Create staging directory adjacent to target
        parent = target_path.parent
        stage_name = f"{self._reserved_prefix}{target_path.name}"

        return parent / stage_name

    def stage_content(
        self,
        target_path: Path,
        content: bytes,
        fsync: bool = True,
    ) -> Path:
        """Stage content to an adjacent temporary file with optional fsync."""
        stage_path = self.get_stage_path(target_path)

        # Ensure parent directory exists
        stage_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to temporary file first
        temp_suffix = ".tmp"
        temp_path = stage_path.with_suffix(stage_path.suffix + temp_suffix)

        try:
            with open(temp_path, "wb") as f:
                f.write(content)

                if fsync:
                    f.flush()
                    os.fsync(f.fileno())

            # Atomic rename to stage path
            temp_path.replace(stage_path)

            return stage_path

        except Exception:
            # Clean up temp file on failure
            if temp_path.exists():
                temp_path.unlink()
            raise

    def stage_file_copy(
        self,
        source_path: Path,
        target_path: Path,
        fsync: bool = True,
    ) -> Path:
        """Create a staged copy of source file for target."""
        if not source_path.exists():
            raise FileNotFoundError(f"Source file not found: {source_path}")

        stage_path = self.get_stage_path(target_path)

        # Copy file to staging location
        with open(source_path, "rb") as src:
            content = src.read()

        return self.stage_content(target_path, content, fsync)

    def atomic_replace(self, stage_path: Path, target_path: Path) -> None:
        """Atomically replace target file with staged content."""
        if not stage_path.exists():
            raise FileNotFoundError(f"Stage file not found: {stage_path}")

        # Ensure target directory exists
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic rename
        stage_path.replace(target_path)

    def atomic_stage_and_replace(
        self,
        target_path: Path,
        content: bytes,
        fsync: bool = True,
    ) -> None:
        """One-shot stage and atomic replace operation."""
        stage_path = self.stage_content(target_path, content, fsync)
        self.atomic_replace(stage_path, target_path)

    def cleanup_stage(self, target_path: Path) -> None:
        """Remove staging file for target if it exists."""
        stage_path = self.get_stage_path(target_path)

        if stage_path.exists():
            stage_path.unlink()

    def cleanup_all_stages(self, directory: Path) -> int:
        """Remove all staging files in a directory and return count removed."""
        count = 0

        if not directory.exists() or not directory.is_dir():
            return count

        for item in directory.iterdir():
            if item.name.startswith(self._reserved_prefix):
                if item.is_file():
                    item.unlink()
                    count += 1
                elif item.is_dir():
                    # Recursively clean up staging directories
                    count += self.cleanup_all_stages(item)
                    if not any(item.iterdir()):
                        item.rmdir()

        return count

    def has_stage(self, target_path: Path) -> bool:
        """Check if staging file exists for target."""
        stage_path = self.get_stage_path(target_path)
        return stage_path.exists()

    def get_stage_size(self, target_path: Path) -> int | None:
        """Get size of staged file if it exists."""
        stage_path = self.get_stage_path(target_path)

        if stage_path.exists() and stage_path.is_file():
            return stage_path.stat().st_size

        return None

    def verify_stage_integrity(
        self,
        target_path: Path,
        expected_size: int,
    ) -> bool:
        """Verify staged file has expected size."""
        stage_size = self.get_stage_size(target_path)

        return stage_size is not None and stage_size == expected_size

    def create_temp_stage(self) -> BinaryIO:
        """Create a temporary staging file in the system temp directory."""
        # Create a temporary file with our prefix
        return tempfile.NamedTemporaryFile(
            prefix=self._reserved_prefix,
            delete=False,
        )

    def supports_fsync(self) -> bool:
        """Check if filesystem supports fsync."""
        # Most filesystems support fsync, but we can test
        try:
            with tempfile.NamedTemporaryFile() as f:
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception:
            return False