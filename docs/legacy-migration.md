# Legacy FileRecord Migration

Katsi reads the legacy `file_records.json` source without modifying it. Keep a
copy of that file until a full workspace reconciliation and both graph and
vector projection validation have completed successfully.

Destructive cleanup is intentionally not performed by the importer. Any owner
maintenance flow must pass both safety checks through `LegacyCleanupGuard`
before removing the legacy backup.
