"""Idempotent schema migrations for private authoritative workspace state."""

from __future__ import annotations

import sqlite3

_INITIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_roots (
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    root_path TEXT NOT NULL,
    active INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT,
    PRIMARY KEY (workspace_id, root_path)
);
CREATE TABLE IF NOT EXISTS resources (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    current_path TEXT,
    status TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (workspace_id, current_path)
);
CREATE TABLE IF NOT EXISTS resource_versions (
    id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES resources(id),
    content_hash TEXT NOT NULL,
    byte_count INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    UNIQUE (resource_id, content_hash)
);
CREATE TABLE IF NOT EXISTS workspace_events (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    resource_id TEXT REFERENCES resources(id),
    correlation_id TEXT,
    detail_json TEXT NOT NULL,
    UNIQUE (workspace_id, sequence)
);
CREATE TABLE IF NOT EXISTS content_enrichments (
    content_hash TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    extraction_json TEXT,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (content_hash, fingerprint)
);
CREATE TABLE IF NOT EXISTS agent_identities (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    client_name TEXT NOT NULL,
    model_name TEXT,
    process_description TEXT,
    active INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_credentials (
    id TEXT PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES agent_identities(id),
    credential_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS capability_grants (
    id TEXT PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES agent_identities(id),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    operation_classes_json TEXT NOT NULL,
    resource_scope_json TEXT NOT NULL,
    maximum_risk TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    author_id TEXT NOT NULL REFERENCES agent_identities(id),
    text TEXT NOT NULL,
    scope_paths_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_evidence (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    kind TEXT NOT NULL,
    reference_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_transitions (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    actor_id TEXT REFERENCES agent_identities(id),
    occurred_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS open_work (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    author_id TEXT NOT NULL REFERENCES agent_identities(id),
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_leases (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    holder_id TEXT NOT NULL REFERENCES agent_identities(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    task_description TEXT NOT NULL,
    resource_scope_json TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT
);
CREATE TABLE IF NOT EXISTS change_sets (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    author_id TEXT NOT NULL REFERENCES agent_identities(id),
    title TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    risk TEXT NOT NULL,
    status TEXT NOT NULL,
    successor_id TEXT REFERENCES change_sets(id),
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS change_set_dependencies (
    change_set_id TEXT NOT NULL REFERENCES change_sets(id),
    resource_id TEXT NOT NULL REFERENCES resources(id),
    expected_version_id TEXT REFERENCES resource_versions(id),
    expected_content_hash TEXT,
    expected_absent INTEGER NOT NULL,
    PRIMARY KEY (change_set_id, resource_id)
);
CREATE TABLE IF NOT EXISTS change_set_operations (
    id TEXT PRIMARY KEY,
    change_set_id TEXT NOT NULL REFERENCES change_sets(id),
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    operation_json TEXT NOT NULL,
    UNIQUE (change_set_id, ordinal)
);
CREATE TABLE IF NOT EXISTS change_set_transitions (
    id TEXT PRIMARY KEY,
    change_set_id TEXT NOT NULL REFERENCES change_sets(id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    actor_id TEXT REFERENCES agent_identities(id),
    occurred_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_journal (
    id TEXT PRIMARY KEY,
    change_set_id TEXT NOT NULL REFERENCES change_sets(id),
    status TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    recovery_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery_blobs (
    content_hash TEXT PRIMARY KEY,
    byte_count INTEGER NOT NULL,
    storage_path TEXT NOT NULL,
    retained_until TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projection_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    event_id TEXT NOT NULL REFERENCES workspace_events(id),
    projection_name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (event_id, projection_name)
);
CREATE TABLE IF NOT EXISTS projection_offsets (
    projection_name TEXT NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    outbox_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (projection_name, workspace_id)
);
CREATE INDEX IF NOT EXISTS resources_workspace_path_idx ON resources(workspace_id, current_path);
CREATE INDEX IF NOT EXISTS resource_versions_resource_idx ON resource_versions(resource_id, observed_at);
CREATE INDEX IF NOT EXISTS workspace_events_workspace_sequence_idx ON workspace_events(workspace_id, sequence);
CREATE INDEX IF NOT EXISTS projection_outbox_workspace_idx ON projection_outbox(workspace_id, id);
"""

_MIGRATIONS: dict[int, str] = {1: _INITIAL_SCHEMA}

_DURABLE_RECORDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_records (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    author_id TEXT NOT NULL REFERENCES agent_identities(id),
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_record_transitions (
    id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES workspace_records(id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES agent_identities(id),
    occurred_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS open_work_transitions (
    id TEXT PRIMARY KEY,
    open_work_id TEXT NOT NULL REFERENCES open_work(id),
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES agent_identities(id),
    occurred_at TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS workspace_records_workspace_idx ON workspace_records(workspace_id, kind, status);
CREATE INDEX IF NOT EXISTS open_work_workspace_idx ON open_work(workspace_id, status);
"""

_MIGRATIONS[2] = _DURABLE_RECORDS_SCHEMA

_INTENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_intents (
    workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id),
    goal TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_MIGRATIONS[3] = _INTENT_SCHEMA

_GOVERNED_EXECUTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotent_operations (
    execution_id TEXT PRIMARY KEY,
    change_set_id TEXT NOT NULL REFERENCES change_sets(id),
    operation_kind TEXT NOT NULL,
    operation_json TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    steps_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine_records (
    id TEXT PRIMARY KEY,
    original_path TEXT NOT NULL,
    quarantine_path TEXT NOT NULL,
    quarantine_timestamp TEXT NOT NULL,
    content_hash TEXT,
    action_history_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    quarantine_size INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idempotent_operations_change_set_idx ON idempotent_operations(change_set_id);
CREATE INDEX IF NOT EXISTS idempotent_operations_status_idx ON idempotent_operations(status);
CREATE INDEX IF NOT EXISTS quarantine_records_timestamp_idx ON quarantine_records(quarantine_timestamp);
"""

_MIGRATIONS[4] = _GOVERNED_EXECUTION_SCHEMA

_VALIDATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS change_set_validations (
    id TEXT PRIMARY KEY,
    change_set_id TEXT NOT NULL REFERENCES change_sets(id),
    validation_result_json TEXT NOT NULL,
    validated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_set_staleness_triggers (
    id TEXT PRIMARY KEY,
    change_set_id TEXT NOT NULL REFERENCES change_sets(id),
    triggering_event_id TEXT NOT NULL REFERENCES workspace_events(id),
    triggered_at TEXT NOT NULL,
    UNIQUE (change_set_id, triggering_event_id)
);
CREATE TABLE IF NOT EXISTS owner_decisions (
    decision_id TEXT PRIMARY KEY,
    change_set_id TEXT NOT NULL REFERENCES change_sets(id),
    decision TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES agent_identities(id),
    decided_at TEXT NOT NULL,
    reason TEXT,
    evidence_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS change_set_validations_change_set_idx ON change_set_validations(change_set_id, validated_at);
CREATE INDEX IF NOT EXISTS change_set_staleness_triggers_change_set_idx ON change_set_staleness_triggers(change_set_id);
CREATE INDEX IF NOT EXISTS owner_decisions_change_set_idx ON owner_decisions(change_set_id);
"""

_MIGRATIONS[4] = _VALIDATION_SCHEMA

_YOLO_SCHEMA = """
CREATE TABLE IF NOT EXISTS yolo_modes (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    owner_identity_id TEXT NOT NULL REFERENCES agent_identities(id),
    agent_identity_id TEXT NOT NULL REFERENCES agent_identities(id),
    policy_version TEXT NOT NULL,
    operation_classes_json TEXT NOT NULL,
    resource_scope_json TEXT NOT NULL,
    maximum_risk TEXT NOT NULL,
    allow_derived_artifacts INTEGER NOT NULL,
    allow_reversible_organization INTEGER NOT NULL,
    require_owner_approval_for_originals INTEGER NOT NULL,
    status TEXT NOT NULL,
    activated_at TEXT,
    suspended_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS yolo_authorizations (
    id TEXT PRIMARY KEY,
    yolo_mode_id TEXT NOT NULL REFERENCES yolo_modes(id),
    change_set_id TEXT NOT NULL REFERENCES change_sets(id),
    auto_authorized INTEGER NOT NULL,
    policy_matched TEXT NOT NULL,
    authorized_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS yolo_suspension_events (
    id TEXT PRIMARY KEY,
    yolo_mode_id TEXT NOT NULL REFERENCES yolo_modes(id),
    suspension_reason TEXT NOT NULL,
    related_change_set_id TEXT REFERENCES change_sets(id),
    related_event_id TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS yolo_modes_workspace_idx ON yolo_modes(workspace_id, status);
CREATE INDEX IF NOT EXISTS yolo_authorizations_mode_idx ON yolo_authorizations(yolo_mode_id);
CREATE INDEX IF NOT EXISTS yolo_suspension_events_mode_idx ON yolo_suspension_events(yolo_mode_id);
"""

_MIGRATIONS[4] = _YOLO_SCHEMA


def apply_migrations(connection: sqlite3.Connection, target_version: int) -> None:
    """Apply every pending schema migration in one short SQLite transaction."""
    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current_version > target_version:
        raise ValueError(f"database schema {current_version} is newer than target {target_version}")
    for version in range(current_version + 1, target_version + 1):
        try:
            migration = _MIGRATIONS[version]
        except KeyError as error:
            raise ValueError(f"missing schema migration for version {version}") from error
        try:
            connection.executescript(
                f"BEGIN IMMEDIATE;\n{migration}\nPRAGMA user_version = {version};\nCOMMIT;"
            )
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
