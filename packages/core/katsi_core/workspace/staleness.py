"""Staleness detection and marking for Change Sets."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.contracts import (
    ChangeSet,
    ChangeSetId,
    ChangeSetStatus,
    WorkspaceEvent,
    WorkspaceEventKind,
)
from katsi_core.workspace.errors import ConflictError

logger = logging.getLogger(__name__)


class StaleTrigger:
    """Represents a specific event that triggered staleness."""

    def __init__(
        self,
        event_id: UUID,
        event_kind: WorkspaceEventKind,
        resource_id: UUID | None,
        occurred_at: datetime,
        is_relevant: bool,
        correlation_id: UUID | None = None,
        detail: dict[str, str] | None = None,
    ) -> None:
        self.event_id = event_id
        self.event_kind = event_kind
        self.resource_id = resource_id
        self.occurred_at = occurred_at
        self.is_relevant = is_relevant
        self.correlation_id = correlation_id
        self.detail = detail or {}

    def to_dict(self) -> dict[str, object]:
        """Convert trigger to serializable dictionary."""
        return {
            "event_id": str(self.event_id),
            "event_kind": self.event_kind.value,
            "resource_id": str(self.resource_id) if self.resource_id else None,
            "occurred_at": self.occurred_at.isoformat(),
            "is_relevant": self.is_relevant,
            "correlation_id": str(self.correlation_id) if self.correlation_id else None,
            "detail": self.detail,
        }


class StalenessService:
    """Detects and marks Change Sets as stale based on exact triggering events."""

    def __init__(self, database: WorkspaceSQLite) -> None:
        self._database = database

    def detect_staleness(
        self, change_set: ChangeSet, since_sequence: int | None = None
    ) -> tuple[StaleTrigger, ...]:
        """
        Detect staleness-triggering events for a Change Set.
        Returns all relevant events that would make the Change Set stale.
        """
        triggers: list[StaleTrigger] = []

        # Get the sequence number at which the Change Set was last validated
        last_validated_sequence = self._get_validated_sequence(change_set.id)

        # Determine starting sequence for event scan
        start_sequence = since_sequence or last_validated_sequence

        with self._database.connection() as connection:
            # Get events since the last validation
            events = connection.execute(
                """SELECT * FROM workspace_events
                   WHERE workspace_id = ? AND sequence > ?
                   ORDER BY sequence ASC""",
                (str(change_set.workspace_id), start_sequence),
            ).fetchall()

            for event_row in events:
                event = WorkspaceEvent(
                    id=UUID(event_row["id"]),
                    workspace_id=UUID(event_row["workspace_id"]),
                    sequence=event_row["sequence"],
                    kind=WorkspaceEventKind(event_row["kind"]),
                    occurred_at=datetime.fromisoformat(event_row["occurred_at"]),
                    resource_id=UUID(event_row["resource_id"]) if event_row["resource_id"] else None,
                    correlation_id=UUID(event_row["correlation_id"]) if event_row["correlation_id"] else None,
                    detail=json.loads(event_row["detail_json"]),
                )

                # Check if this event is relevant to the Change Set
                is_relevant = self._is_event_relevant_to_change_set(event, change_set)

                trigger = StaleTrigger(
                    event_id=event.id,
                    event_kind=event.kind,
                    resource_id=event.resource_id,
                    occurred_at=event.occurred_at,
                    is_relevant=is_relevant,
                    correlation_id=event.correlation_id,
                    detail=event.detail,
                )

                triggers.append(trigger)

        return tuple(triggers)

    def mark_stale_if_needed(
        self, change_set_id: UUID, triggering_event_id: UUID
    ) -> bool:
        """
        Mark a Change Set as stale if a triggering event occurred.
        Returns True if the Change Set was marked stale.
        """
        change_set = self._get_change_set(change_set_id)
        if change_set is None:
            raise ConflictError(f"Change Set not found: {change_set_id}")

        if change_set.status not in (ChangeSetStatus.PROPOSED, ChangeSetStatus.VALIDATED):
            return False

        # Get the triggering event
        with self._database.connection() as connection:
            event_row = connection.execute(
                "SELECT * FROM workspace_events WHERE id = ?",
                (str(triggering_event_id),),
            ).fetchone()

            if event_row is None:
                logger.warning(f"Triggering event not found: {triggering_event_id}")
                return False

            event = WorkspaceEvent(
                id=UUID(event_row["id"]),
                workspace_id=UUID(event_row["workspace_id"]),
                sequence=event_row["sequence"],
                kind=WorkspaceEventKind(event_row["kind"]),
                occurred_at=datetime.fromisoformat(event_row["occurred_at"]),
                resource_id=UUID(event_row["resource_id"]) if event_row["resource_id"] else None,
                correlation_id=UUID(event_row["correlation_id"]) if event_row["correlation_id"] else None,
                detail=json.loads(event_row["detail_json"]),
            )

            # Check if event is relevant
            is_relevant = self._is_event_relevant_to_change_set(event, change_set)

            if not is_relevant:
                # Unrelated event - don't mark stale
                logger.info(
                    f"Event {triggering_event_id} is unrelated to Change Set {change_set_id}"
                )
                return False

            # Mark the Change Set as stale
            self._record_staleness_trigger(change_set_id, triggering_event_id)

            # Update the Change Set status
            from katsi_core.workspace.change_sets import ChangeSetService

            ChangeSetService(self._database).transition(
                change_set_id,
                ChangeSetStatus.STALE,
                evidence={
                    "triggering_event_id": str(triggering_event_id),
                    "event_kind": event.kind.value,
                    "triggered_at": datetime.now(UTC).isoformat(),
                },
            )

            logger.info(
                f"Marked Change Set {change_set_id} as stale due to event {triggering_event_id}"
            )
            return True

    def get_staleness_triggers(self, change_set_id: UUID) -> tuple[StaleTrigger, ...]:
        """Get all staleness triggers for a Change Set."""
        triggers: list[StaleTrigger] = []

        with self._database.connection() as connection:
            rows = connection.execute(
                """SELECT cst.*, we.*
                   FROM change_set_staleness_triggers cst
                   JOIN workspace_events we ON cst.triggering_event_id = we.id
                   WHERE cst.change_set_id = ?
                   ORDER BY we.occurred_at ASC""",
                (str(change_set_id),),
            ).fetchall()

            for row in rows:
                trigger = StaleTrigger(
                    event_id=UUID(row["id"]),
                    event_kind=WorkspaceEventKind(row["kind"]),
                    resource_id=UUID(row["resource_id"]) if row["resource_id"] else None,
                    occurred_at=datetime.fromisoformat(row["occurred_at"]),
                    is_relevant=True,  # By definition if it's a trigger, it's relevant
                    correlation_id=UUID(row["correlation_id"]) if row["correlation_id"] else None,
                    detail=json.loads(row["detail_json"]),
                )
                triggers.append(trigger)

        return tuple(triggers)

    def clear_staleness_triggers(self, change_set_id: UUID) -> None:
        """Clear all staleness triggers for a Change Set."""
        with self._database.connection() as connection:
            connection.execute(
                "DELETE FROM change_set_staleness_triggers WHERE change_set_id = ?",
                (str(change_set_id),),
            )

    def _is_event_relevant_to_change_set(
        self, event: WorkspaceEvent, change_set: ChangeSet
    ) -> bool:
        """
        Determine if a workspace event is relevant to a Change Set.
        Returns True if the event affects resources the Change Set depends on.
        """
        # Check if the event affects any dependency
        for dependency in change_set.dependencies:
            if event.resource_id == dependency.resource_id:
                return True

            # Check for events that affect dependent paths
            if event.kind in (
                WorkspaceEventKind.RESOURCE_CREATED,
                WorkspaceEventKind.RESOURCE_UPDATED,
                WorkspaceEventKind.RESOURCE_DELETED,
                WorkspaceEventKind.RESOURCE_MOVED,
            ):
                if event.resource_id:
                    # Check if this event's resource is in our dependency graph
                    if self._is_resource_in_dependency_graph(
                        event.resource_id, change_set, event.workspace_id
                    ):
                        return True

        # External changes are always relevant
        if event.kind == WorkspaceEventKind.EXTERNAL_CHANGE:
            return True

        return False

    def _is_resource_in_dependency_graph(
        self, resource_id: UUID, change_set: ChangeSet, workspace_id: UUID
    ) -> bool:
        """Check if a resource is in the Change Set's dependency graph."""
        # Direct dependency check
        for dep in change_set.dependencies:
            if dep.resource_id == resource_id:
                return True

        # For a more sophisticated implementation, we would check:
        # - Resources affected by operations (e.g., replace_file operations)
        # - Transitive dependencies
        # - Parent/child directory relationships

        # Check operations that reference this resource
        with self._database.connection() as connection:
            resource_row = connection.execute(
                "SELECT current_path FROM resources WHERE id = ?",
                (str(resource_id),),
            ).fetchone()

            if resource_row is None:
                return False

            resource_path = resource_row["current_path"]

            for operation in change_set.operations:
                if hasattr(operation, "path") and operation.path == resource_path:
                    return True
                if hasattr(operation, "source_path") and operation.source_path == resource_path:
                    return True
                if hasattr(operation, "destination_path") and operation.destination_path == resource_path:
                    return True

        return False

    def _get_change_set(self, change_set_id: UUID) -> ChangeSet | None:
        """Retrieve a Change Set by ID."""
        from katsi_core.workspace.change_sets import ChangeSetService

        return ChangeSetService(self._database).get(change_set_id)

    def _get_validated_sequence(self, change_set_id: UUID) -> int:
        """Get the workspace event sequence at which a Change Set was validated."""
        with self._database.connection() as connection:
            row = connection.execute(
                """SELECT we.sequence
                   FROM change_set_transitions cst
                   JOIN workspace_events we ON cst.evidence_json LIKE '%' || we.id || '%'
                   WHERE cst.change_set_id = ? AND cst.to_status = ?
                   ORDER BY cst.occurred_at DESC
                   LIMIT 1""",
                (str(change_set_id), ChangeSetStatus.VALIDATED.value),
            ).fetchone()

            if row is None:
                # Never validated, return 0 to check from the beginning
                return 0

            return row["sequence"]

    def _record_staleness_trigger(
        self, change_set_id: UUID, triggering_event_id: UUID
    ) -> None:
        """Record a staleness trigger for a Change Set."""
        with self._database.connection() as connection:
            # Create table if it doesn't exist (this should be in migrations)
            connection.execute(
                """CREATE TABLE IF NOT EXISTS change_set_staleness_triggers (
                    id TEXT PRIMARY KEY,
                    change_set_id TEXT NOT NULL,
                    triggering_event_id TEXT NOT NULL,
                    triggered_at TEXT NOT NULL,
                    UNIQUE (change_set_id, triggering_event_id)
                )"""
            )

            connection.execute(
                """INSERT OR IGNORE INTO change_set_staleness_triggers VALUES (?, ?, ?, ?)""",
                (
                    str(uuid4()),
                    str(change_set_id),
                    str(triggering_event_id),
                    datetime.now(UTC).isoformat(),
                ),
            )
