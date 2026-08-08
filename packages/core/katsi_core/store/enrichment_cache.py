"""Path-independent SQLite cache for compatible local enrichment."""

from __future__ import annotations

import json

from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.enrichment import EnrichmentFingerprint


class EnrichmentCache:
    def __init__(self, database: WorkspaceSQLite) -> None:
        self._database = database

    def get(self, fingerprint: EnrichmentFingerprint) -> dict[str, object] | None:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT extraction_json FROM content_enrichments WHERE content_hash = ? AND fingerprint = ? AND status = 'success'",
                (fingerprint.content_hash, fingerprint.digest()),
            ).fetchone()
        return json.loads(row["extraction_json"]) if row else None

    def put(self, fingerprint: EnrichmentFingerprint, extraction: dict[str, object]) -> None:
        with self._database.connection() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO content_enrichments
                VALUES (?, ?, ?, 'success', NULL, datetime('now'))""",
                (
                    fingerprint.content_hash,
                    fingerprint.digest(),
                    json.dumps(extraction, sort_keys=True),
                ),
            )

    def put_error(self, fingerprint: EnrichmentFingerprint, error: str) -> None:
        """Persist a terminal extraction failure that cannot be read as enrichment."""
        with self._database.connection() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO content_enrichments
                VALUES (?, ?, NULL, 'error', ?, datetime('now'))""",
                (fingerprint.content_hash, fingerprint.digest(), error),
            )
