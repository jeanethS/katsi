"""Configured SQLite connections for the authoritative workspace store."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from katsi_core.config import SQLiteSettings
from katsi_core.workspace.errors import UnsupportedOperationError


class WorkspaceSQLite:
    """Owns process-local SQLite connections and their required pragmas.

    The factory deliberately does not cache a connection per path: callers that
    need concurrent writers receive independent connections, while this object
    still provides deterministic cleanup for the connections it creates.
    """

    def __init__(self, database_path: Path, settings: SQLiteSettings) -> None:
        self._database_path = database_path
        self._settings = settings
        self._connections: set[sqlite3.Connection] = set()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def connect(self) -> sqlite3.Connection:
        """Open a configured connection without beginning a transaction."""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._database_path,
            timeout=self._settings.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA busy_timeout = {self._settings.busy_timeout_ms}")
        self._refuse_newer_schema(connection)
        self._connections.add(connection)
        return connection

    def _refuse_newer_schema(self, connection: sqlite3.Connection) -> None:
        """Refuse state newer than this binary; migrations own version updates."""
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version > self._settings.schema_version:
            connection.close()
            raise UnsupportedOperationError(
                f"database schema {current_version} is newer than supported {self._settings.schema_version}"
            )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield one configured connection and close it at context exit."""
        connection = self.connect()
        try:
            yield connection
        finally:
            self.close(connection)

    def close(self, connection: sqlite3.Connection) -> None:
        """Close a factory-created connection. Repeated cleanup is harmless."""
        if connection in self._connections:
            self._connections.remove(connection)
            connection.close()

    def close_all(self) -> None:
        """Close every process-local connection created by this factory."""
        for connection in tuple(self._connections):
            self.close(connection)
