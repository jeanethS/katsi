"""Tests for Workspace Brief assembly (OpenSpec tasks 10.3 and 10.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from katsi_core.config import (
    BriefSettings,
    LeaseSettings,
    ProjectionWorkerSettings,
    SQLiteSettings,
)
from katsi_core.store.projection_worker import ProjectionWorker
from katsi_core.store.workspace_migrations import apply_migrations
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.brief import BriefService
from katsi_core.workspace.budget import SerializedBudgeter
from katsi_core.workspace.claims import ClaimService
from katsi_core.workspace.contracts import (
    BriefClaim,
    BriefSection,
    Claim,
    ClaimEvidence,
    ClaimEvidenceKind,
    ClaimStatus,
    OpenWork,
    OpenWorkStatus,
    WorkspaceEventKind,
    WorkspaceRecord,
    WorkspaceRecordKind,
    WorkspaceRecordStatus,
)
from katsi_core.workspace.errors import WorkspaceError
from katsi_core.workspace.identity import IdentityService
from katsi_core.workspace.intent import IntentService
from katsi_core.workspace.leases import WorkLeaseService
from katsi_core.workspace.records import WorkspaceRecordService

_BUDGET = 1_000_000


def _build(tmp_path: Path) -> tuple:
    database = WorkspaceSQLite(tmp_path / "workspace.sqlite3", SQLiteSettings())
    with database.connection() as connection:
        apply_migrations(connection, target_version=3)
    root = tmp_path / "project"
    root.mkdir()
    repository = WorkspaceRepository(database)
    workspace = repository.register_workspace(root, "Project")
    identities = IdentityService(database)
    intents = IntentService(database)
    claims = ClaimService(database, identities)
    records = WorkspaceRecordService(database, identities)
    leases = WorkLeaseService(database, identities, LeaseSettings(advisory_ttl_seconds=60))
    author = identities.register("Agent", "test")
    brief = BriefService(repository, database, intents, claims, records, leases, BriefSettings())
    return workspace, author, intents, claims, records, leases, brief, repository, database


def _verify_claim(claims: ClaimService, claim: Claim, author) -> None:
    evidence = ClaimEvidence(
        id=uuid4(),
        claim_id=claim.id,
        kind=ClaimEvidenceKind.DETERMINISTIC,
        reference={"verifier": "unit-test"},
        created_at=datetime.now(UTC),
    )
    claims.transition(claim.id, author.id, ClaimStatus.VERIFIED, evidence)


def _append(repository: WorkspaceRepository, workspace, kind, **kwargs):
    version = repository.get_workspace(workspace.id).state_version
    return repository.append_event(workspace.id, version, kind, **kwargs)


def _claim_cost(claim: Claim, author) -> int:
    model = BriefClaim(
        id=claim.id,
        text=claim.text,
        author_id=author.id,
        status=claim.status,
        confidence=claim.confidence,
        scope_paths=claim.scope_paths,
        created_at=claim.created_at,
        invalidated=claim.status is ClaimStatus.INVALIDATED,
    )
    return SerializedBudgeter().serialized_bytes(model.model_dump(mode="json"))


def test_brief_assembles_authoritative_state_with_provenance(tmp_path: Path) -> None:
    (
        workspace,
        author,
        intents,
        claims,
        records,
        leases,
        brief,
        _repository,
        _database,
    ) = _build(tmp_path)
    intents.activate(workspace.id, "Ship workspace coordination")
    verified = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        text="SQLite is the private authority.",
        confidence=0.9,
        created_at=datetime.now(UTC),
    )
    proposed = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        text="Maybe add a verifier.",
        confidence=0.4,
        created_at=datetime.now(UTC),
    )
    claims.publish(verified)
    _verify_claim(claims, verified, author)
    claims.publish(proposed)

    now = datetime.now(UTC)
    decision = WorkspaceRecord(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        kind=WorkspaceRecordKind.DECISION,
        text="Use serialized budget accounting.",
        created_at=now,
        updated_at=now,
    )
    blocker = WorkspaceRecord(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        kind=WorkspaceRecordKind.BLOCKER,
        text="Await owner approval.",
        created_at=now,
        updated_at=now,
    )
    question = WorkspaceRecord(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        kind=WorkspaceRecordKind.OPEN_QUESTION,
        text="Which projection first?",
        created_at=now,
        updated_at=now,
    )
    for record in (decision, blocker, question):
        records.publish_record(record)
    records.transition_record(decision.id, author.id, WorkspaceRecordStatus.VERIFIED)
    work = OpenWork(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        description="Assemble the brief.",
        created_at=now,
        updated_at=now,
    )
    records.create_open_work(work)
    leases.acquire(workspace.id, author.id, "Explore briefs", ("src",))

    result = brief.assemble(workspace.id, byte_budget=_BUDGET)

    assert result.intent == ("Ship workspace coordination", 1)
    assert [c.id for c in result.claims] == [verified.id, proposed.id]
    assert result.claims[0].status is ClaimStatus.VERIFIED
    assert result.claims[0].author_id == author.id  # provenance
    assert result.claims[1].status is ClaimStatus.PROPOSED
    assert [d.id for d in result.decisions] == [decision.id]
    assert [b.id for b in result.blockers] == [blocker.id]
    assert [q.id for q in result.open_questions] == [question.id]
    assert [w.id for w in result.open_work] == [work.id]
    assert len(result.leases) == 1
    assert result.bytes_used <= result.budget_bytes
    assert result.omitted == ()
    assert BriefSection.CLAIM in result.provisional
    assert BriefSection.OPEN_WORK in result.provisional
    assert BriefSection.DECISION not in result.provisional
    assert result.projection_lag is False


def test_brief_excludes_resolved_and_terminal_state(tmp_path: Path) -> None:
    (
        workspace,
        author,
        intents,
        claims,
        records,
        _leases,
        brief,
        _repository,
        _database,
    ) = _build(tmp_path)
    intents.activate(workspace.id, "Goal")
    contradicted = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        text="Rejected assertion.",
        confidence=0.5,
        created_at=datetime.now(UTC),
    )
    claims.publish(contradicted)
    claims.transition(contradicted.id, author.id, ClaimStatus.CONTRADICTED)

    now = datetime.now(UTC)
    decision = WorkspaceRecord(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        kind=WorkspaceRecordKind.DECISION,
        text="Resolved decision.",
        created_at=now,
        updated_at=now,
    )
    records.publish_record(decision)
    records.transition_record(decision.id, author.id, WorkspaceRecordStatus.RESOLVED)
    completed = OpenWork(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        description="Done work.",
        created_at=now,
        updated_at=now,
    )
    records.create_open_work(completed)
    records.transition_open_work(completed.id, author.id, OpenWorkStatus.COMPLETED)

    result = brief.assemble(workspace.id, byte_budget=_BUDGET)

    assert result.claims == ()
    assert result.decisions == ()
    assert result.open_work == ()
    omitted_sections = {om.section for om in result.omitted}
    assert BriefSection.CLAIM not in omitted_sections
    assert BriefSection.DECISION not in omitted_sections


def test_brief_enforces_serialized_byte_budget_with_explicit_omissions(
    tmp_path: Path,
) -> None:
    (
        workspace,
        author,
        intents,
        claims,
        _records,
        _leases,
        brief,
        _repository,
        _database,
    ) = _build(tmp_path)

    intents.activate(workspace.id, "G")
    verified = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        text="V",
        confidence=0.9,
        created_at=datetime.now(UTC),
    )
    proposed = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        text="P" * 500,  # large enough that it cannot fit alongside the goal + verified claim
        confidence=0.3,
        created_at=datetime.now(UTC),
    )
    claims.publish(verified)
    _verify_claim(claims, verified, author)
    claims.publish(proposed)

    goal_cost = SerializedBudgeter().serialized_bytes({"goal": "G", "version": 1})
    byte_budget = goal_cost + _claim_cost(verified, author)

    result = brief.assemble(workspace.id, byte_budget=byte_budget)

    assert result.budget_bytes == byte_budget
    assert result.bytes_used == byte_budget
    assert result.intent == ("G", 1)
    assert [c.id for c in result.claims] == [verified.id]  # proposed omitted by budget
    claim_omission = next(om for om in result.omitted if om.section is BriefSection.CLAIM)
    assert claim_omission.count == 1
    assert claim_omission.reason == "budget"


def test_brief_reports_recent_workspace_events(tmp_path: Path) -> None:
    (
        workspace,
        _author,
        intents,
        _claims,
        _records,
        _leases,
        brief,
        repository,
        _database,
    ) = _build(tmp_path)
    intents.activate(workspace.id, "Goal")
    _append(repository, workspace, WorkspaceEventKind.EXTERNAL_CHANGE, detail={"path": "src/a.py"})
    _append(repository, workspace, WorkspaceEventKind.EXTERNAL_CHANGE, detail={"path": "src/b.py"})

    result = brief.assemble(workspace.id, byte_budget=_BUDGET)

    external = [e for e in result.recent_events if e.kind is WorkspaceEventKind.EXTERNAL_CHANGE]
    assert {e.path for e in external} == {"src/a.py", "src/b.py"}
    assert all(e.event_sequence >= 1 for e in external)


def test_brief_labels_invalidated_claims_as_stale_context(tmp_path: Path) -> None:
    (
        workspace,
        author,
        intents,
        claims,
        _records,
        _leases,
        brief,
        _repository,
        _database,
    ) = _build(tmp_path)
    intents.activate(workspace.id, "Goal")

    stable = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        text="Stable verified knowledge.",
        confidence=0.8,
        created_at=datetime.now(UTC),
    )
    claims.publish(stable)
    _verify_claim(claims, stable, author)

    resource_id = uuid4()
    stale = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        text="Stale verified knowledge.",
        confidence=0.8,
        created_at=datetime.now(UTC),
    )
    claims.publish(
        stale,
        (
            ClaimEvidence(
                id=uuid4(),
                claim_id=stale.id,
                kind=ClaimEvidenceKind.RESOURCE_VERSION,
                reference={"resource_id": str(resource_id), "version_id": str(uuid4())},
                created_at=datetime.now(UTC),
            ),
        ),
    )
    claims.transition(
        stale.id,
        author.id,
        ClaimStatus.VERIFIED,
        ClaimEvidence(
            id=uuid4(),
            claim_id=stale.id,
            kind=ClaimEvidenceKind.DETERMINISTIC,
            reference={"resource_id": str(resource_id), "verifier": "unit-test"},
            created_at=datetime.now(UTC),
        ),
    )
    assert claims.invalidate_resource_evidence(workspace.id, resource_id)

    full = brief.assemble(workspace.id, byte_budget=_BUDGET)
    stable_brief = next(c for c in full.claims if c.id == stable.id)
    stale_brief = next(c for c in full.claims if c.id == stale.id)
    assert stable_brief.invalidated is False
    assert stale_brief.status is ClaimStatus.INVALIDATED
    assert stale_brief.invalidated is True
    assert BriefSection.CLAIM in full.provisional  # invalidated context is provisional/stale

    # Under a tight budget the verified claim is kept and the invalidated one omitted.
    goal_cost = SerializedBudgeter().serialized_bytes({"goal": "Goal", "version": 1})
    tight = brief.assemble(workspace.id, byte_budget=goal_cost + _claim_cost(stable, author))
    assert {c.id for c in tight.claims} == {stable.id}
    assert "claim" in {om.section for om in tight.omitted}


def test_brief_reports_projection_lag_and_clears_when_caught_up(tmp_path: Path) -> None:
    (
        workspace,
        _author,
        _intents,
        _claims,
        _records,
        _leases,
        brief,
        repository,
        database,
    ) = _build(tmp_path)
    _append(
        repository,
        workspace,
        WorkspaceEventKind.RESOURCE_UPDATED,
        projection_payloads={"graph": {"action": "replace"}, "vector": {"action": "replace"}},
    )

    lagging = brief.assemble(workspace.id, byte_budget=_BUDGET)
    freshness = {f.projection_name: f for f in lagging.projection_freshness}
    assert set(freshness) == {"graph", "vector"}
    assert all(f.lagging for f in freshness.values())
    assert lagging.projection_lag is True

    worker = ProjectionWorker(database, ProjectionWorkerSettings())
    for name in ("graph", "vector"):
        while worker.run(workspace.id, name, lambda _entry: None) > 0:
            pass

    caught_up = brief.assemble(workspace.id, byte_budget=_BUDGET)
    freshness = {f.projection_name: f for f in caught_up.projection_freshness}
    assert all(f.lag == 0 and not f.lagging for f in freshness.values())
    assert caught_up.projection_lag is False


def test_authoritative_claim_operation_succeeds_while_projections_lag(tmp_path: Path) -> None:
    """Projection lag reduces retrieval freshness but never blocks durable state writes."""
    (
        workspace,
        author,
        _intents,
        claims,
        _records,
        _leases,
        brief,
        repository,
        _database,
    ) = _build(tmp_path)
    _append(
        repository,
        workspace,
        WorkspaceEventKind.RESOURCE_UPDATED,
        projection_payloads={"graph": {"action": "replace"}, "vector": {"action": "replace"}},
    )
    assert brief.assemble(workspace.id, byte_budget=_BUDGET).projection_lag is True

    claim = Claim(
        id=uuid4(),
        workspace_id=workspace.id,
        author_id=author.id,
        text="Authoritative SQLite operations remain available during projection lag.",
        confidence=0.7,
        created_at=datetime.now(UTC),
    )
    claims.publish(claim)

    result = brief.assemble(workspace.id, byte_budget=_BUDGET)
    assert result.projection_lag is True
    assert [included.id for included in result.claims] == [claim.id]


def test_brief_unknown_workspace_raises(tmp_path: Path) -> None:
    (
        _workspace,
        _author,
        _intents,
        _claims,
        _records,
        _leases,
        brief,
        _repository,
        _database,
    ) = _build(tmp_path)
    with pytest.raises(WorkspaceError):
        brief.assemble(uuid4(), byte_budget=_BUDGET)


def test_brief_rejects_negative_budget(tmp_path: Path) -> None:
    (
        workspace,
        _author,
        _intents,
        _claims,
        _records,
        _leases,
        brief,
        _repository,
        _database,
    ) = _build(tmp_path)
    with pytest.raises(ValueError):
        brief.assemble(workspace.id, byte_budget=-1)
