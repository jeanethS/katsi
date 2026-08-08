"""Filesystem observation contracts; optional platform adapters load lazily."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID


class FilesystemEventKind(StrEnum):
    CREATED = "created"
    MODIFIED = "modified"
    MOVED = "moved"
    DELETED = "deleted"
    OVERFLOW = "overflow"


@dataclass(frozen=True, slots=True)
class FilesystemEvent:
    """A lossy observer event used only as a reconciliation hint."""

    kind: FilesystemEventKind
    path: Path
    destination_path: Path | None = None
    source_sequence: int | None = None
    correlation_id: UUID | None = None


class FilesystemObserver(Protocol):
    def start(self, root: Path, on_event: Callable[[FilesystemEvent], None]) -> None: ...

    def stop(self) -> None: ...


class WorkspaceScanner(Protocol):
    def scan(self, root: Path) -> Iterable[Path]: ...


class WatchdogObserver:
    """Cross-platform adapter that only imports watchdog when observation starts."""

    def __init__(self) -> None:
        self._observer: object | None = None

    def start(self, root: Path, on_event: Callable[[FilesystemEvent], None]) -> None:
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError as error:
            raise RuntimeError(
                "watchdog observation requires the optional watchdog dependency"
            ) from error

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event: object) -> None:
                if getattr(event, "is_directory", False):
                    return
                source = getattr(event, "src_path", None)
                if source:
                    event_type = str(getattr(event, "event_type", "modified"))
                    kind = {
                        "created": FilesystemEventKind.CREATED,
                        "modified": FilesystemEventKind.MODIFIED,
                        "moved": FilesystemEventKind.MOVED,
                        "deleted": FilesystemEventKind.DELETED,
                    }.get(event_type, FilesystemEventKind.MODIFIED)
                    destination = getattr(event, "dest_path", None)
                    on_event(
                        FilesystemEvent(
                            kind=kind,
                            path=Path(source),
                            destination_path=Path(destination) if destination else None,
                        )
                    )

        observer = Observer()
        observer.schedule(Handler(), str(root), recursive=True)
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()  # type: ignore[attr-defined]
            self._observer.join()  # type: ignore[attr-defined]
            self._observer = None
