"""Schema-versioned portable workspace state with a strict privacy boundary."""

from __future__ import annotations

import os
from pathlib import Path

from katsi_core.workspace.contracts import PortableProjectState


class PortableStateStore:
    """Read and atomically write only owner-approved portable project state."""

    def __init__(self, relative_path: Path) -> None:
        if relative_path.is_absolute():
            raise ValueError("portable state path must be workspace-relative")
        self._relative_path = relative_path

    def export(self, workspace_root: Path, state: PortableProjectState) -> Path:
        """Atomically write canonical JSON without private operational fields."""
        destination = self._destination(workspace_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_text(state.model_dump_json(indent=None), encoding="utf-8")
        os.replace(temporary, destination)
        return destination

    def import_state(self, workspace_root: Path) -> PortableProjectState:
        """Load a strict portable document; credentials and operations cannot deserialize."""
        return PortableProjectState.model_validate_json(
            self._destination(workspace_root).read_text("utf-8")
        )

    def _destination(self, workspace_root: Path) -> Path:
        root = workspace_root.resolve(strict=True)
        destination = (root / self._relative_path).resolve()
        if not destination.is_relative_to(root):
            raise ValueError("portable state path escapes workspace root")
        return destination
