"""Full-scan convergence for the authoritative workspace model."""

from __future__ import annotations

from pathlib import Path

from katsi_core.config import IngestSettings, ObserverSettings
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.workspace.claims import ClaimService
from katsi_core.workspace.contracts import (
    Resource,
    ResourceId,
    Workspace,
    WorkspaceEventKind,
    WorkspaceId,
)
from katsi_core.workspace.observer import FilesystemEvent, FilesystemEventKind
from katsi_core.workspace.stable_read import stable_content_hash


class WorkspaceReconciler:
    def __init__(
        self,
        repository: WorkspaceRepository,
        ingest: IngestSettings,
        observer: ObserverSettings,
        claims: ClaimService | None = None,
    ) -> None:
        self._repository, self._ingest, self._observer = repository, ingest, observer
        self._claims = claims
        self._last_observer_sequence: dict[WorkspaceId, int] = {}

    def full_scan(self, workspace_id: WorkspaceId) -> None:
        workspace = self._repository.get_workspace(workspace_id)
        if workspace is None:
            raise ValueError(f"unknown workspace {workspace_id}")
        root = Path(workspace.root_path)
        tracked = {
            resource.current_path: resource
            for resource in self._repository.list_current_resources(workspace_id)
        }
        for path in root.rglob("*"):
            digest = stable_content_hash(path, root, self._ingest, self._observer)
            if digest is None:
                continue
            relative = path.relative_to(root).as_posix()
            current = tracked.pop(relative, None)
            workspace = self._repository.get_workspace(workspace_id)
            assert workspace is not None
            if current is None:
                self._repository.create_resource(
                    workspace_id, workspace.state_version, relative, digest, path.stat().st_size
                )
            elif self._repository.current_content_hash(current.id) != digest:
                self._repository.update_resource(
                    workspace_id,
                    workspace.state_version,
                    current.id,
                    current.state_version,
                    digest,
                    path.stat().st_size,
                )
                self._invalidate_claims(workspace_id, current.id)
        for resource in tracked.values():
            workspace = self._repository.get_workspace(workspace_id)
            assert workspace is not None
            self._repository.delete_resource(
                workspace_id, workspace.state_version, resource.id, resource.state_version
            )
            self._invalidate_claims(workspace_id, resource.id)

    def reconcile_hint(self, workspace_id: WorkspaceId, _path: Path) -> None:
        """Apply an observer hint idempotently; a scan owns final filesystem truth."""
        self.full_scan(workspace_id)

    def on_startup(self, workspace_id: WorkspaceId) -> None:
        """Establish filesystem truth before accepting a new observer stream."""
        self.full_scan(workspace_id)

    def request_full_reconciliation(self, workspace_id: WorkspaceId) -> None:
        """Run the owner-requested convergence scan."""
        self.full_scan(workspace_id)

    def handle_event(self, workspace_id: WorkspaceId, event: FilesystemEvent) -> None:
        """Apply an observer event as an idempotent hint, never as final truth.

        Explicit move events retain the resource identity.  Every uncertain event
        falls back to a full scan, which also handles observer coalescing.
        """
        if self._has_sequence_gap(workspace_id, event.source_sequence):
            self.full_scan(workspace_id)
            return
        if event.kind is FilesystemEventKind.OVERFLOW:
            self.full_scan(workspace_id)
            return
        workspace = self._repository.get_workspace(workspace_id)
        if workspace is None:
            raise ValueError(f"unknown workspace {workspace_id}")
        root = Path(workspace.root_path)
        source = self._relative_path(root, event.path)
        if source is None:
            return
        current = self._resources_by_path(workspace_id)
        if event.kind is FilesystemEventKind.MOVED:
            destination = (
                self._relative_path(root, event.destination_path)
                if event.destination_path is not None
                else None
            )
            resource = current.get(source)
            if resource is None or destination is None or destination in current:
                self.full_scan(workspace_id)
                return
            destination_file = root / destination
            if stable_content_hash(destination_file, root, self._ingest, self._observer) is None:
                self.full_scan(workspace_id)
                return
            latest = self._workspace(workspace_id)
            self._repository.move_resource(
                workspace_id,
                latest.state_version,
                resource.id,
                resource.state_version,
                destination,
                event_kind=self._event_kind(event),
                correlation_id=event.correlation_id,
            )
            return
        resource = current.get(source)
        file_path = root / source
        content_hash = stable_content_hash(file_path, root, self._ingest, self._observer)
        if content_hash is None:
            if resource is not None and event.kind in {
                FilesystemEventKind.DELETED,
                FilesystemEventKind.MODIFIED,
            }:
                latest = self._workspace(workspace_id)
                self._repository.delete_resource(
                    workspace_id,
                    latest.state_version,
                    resource.id,
                    resource.state_version,
                    event_kind=self._event_kind(event),
                    correlation_id=event.correlation_id,
                )
                self._invalidate_claims(workspace_id, resource.id)
            return
        if resource is None:
            latest = self._workspace(workspace_id)
            self._repository.create_resource(
                workspace_id,
                latest.state_version,
                source,
                content_hash,
                file_path.stat().st_size,
                event_kind=self._event_kind(event),
                correlation_id=event.correlation_id,
            )
        elif self._repository.current_content_hash(resource.id) != content_hash:
            latest = self._workspace(workspace_id)
            self._repository.update_resource(
                workspace_id,
                latest.state_version,
                resource.id,
                resource.state_version,
                content_hash,
                file_path.stat().st_size,
                event_kind=self._event_kind(event),
                correlation_id=event.correlation_id,
            )
            self._invalidate_claims(workspace_id, resource.id)

    def _has_sequence_gap(self, workspace_id: WorkspaceId, source_sequence: int | None) -> bool:
        if source_sequence is None:
            return False
        previous = self._last_observer_sequence.get(workspace_id)
        self._last_observer_sequence[workspace_id] = source_sequence
        return previous is not None and source_sequence != previous + 1

    @staticmethod
    def _event_kind(event: FilesystemEvent) -> WorkspaceEventKind:
        """Only executor-correlated writes are trusted as governed changes."""
        if event.correlation_id is None:
            return WorkspaceEventKind.EXTERNAL_CHANGE
        return {
            FilesystemEventKind.CREATED: WorkspaceEventKind.RESOURCE_CREATED,
            FilesystemEventKind.MODIFIED: WorkspaceEventKind.RESOURCE_UPDATED,
            FilesystemEventKind.MOVED: WorkspaceEventKind.RESOURCE_MOVED,
            FilesystemEventKind.DELETED: WorkspaceEventKind.RESOURCE_DELETED,
        }.get(event.kind, WorkspaceEventKind.EXTERNAL_CHANGE)

    def _workspace(self, workspace_id: WorkspaceId) -> Workspace:
        workspace = self._repository.get_workspace(workspace_id)
        if workspace is None:
            raise ValueError(f"unknown workspace {workspace_id}")
        return workspace

    def _resources_by_path(self, workspace_id: WorkspaceId) -> dict[str, Resource]:
        return {
            resource.current_path: resource
            for resource in self._repository.list_current_resources(workspace_id)
            if resource.current_path is not None
        }

    def _invalidate_claims(self, workspace_id: WorkspaceId, resource_id: ResourceId) -> None:
        if self._claims is not None:
            self._claims.invalidate_resource_evidence(workspace_id, resource_id)

    @staticmethod
    def _relative_path(root: Path, path: Path) -> str | None:
        try:
            return path.absolute().relative_to(root.absolute()).as_posix()
        except ValueError:
            return None
