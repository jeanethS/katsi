from pathlib import Path

from katsi_core.config import SQLiteSettings
from katsi_core.store.enrichment_cache import EnrichmentCache
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.enrichment import EnrichmentFingerprint


def test_cache_reuses_compatible_content_independent_of_resource_path(tmp_path: Path) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    cache = EnrichmentCache(database)
    compatible = EnrichmentFingerprint(
        content_hash="a" * 64,
        extraction_contract_version="1",
        model_identity="local",
        prompt_version="1",
        chunking_version="1",
        semantic_settings_version="1",
    )
    changed = compatible.model_copy(update={"prompt_version": "2"})

    cache.put(compatible, {"summary": "reused"})

    assert cache.get(compatible) == {"summary": "reused"}
    assert cache.get(changed) is None


def test_terminal_error_is_not_available_as_semantic_enrichment(tmp_path: Path) -> None:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, 1)
    cache = EnrichmentCache(database)
    fingerprint = EnrichmentFingerprint(
        content_hash="b" * 64,
        extraction_contract_version="1",
        model_identity="local",
        prompt_version="1",
        chunking_version="1",
        semantic_settings_version="1",
    )

    cache.put_error(fingerprint, "invalid contract after retry")

    assert cache.get(fingerprint) is None
