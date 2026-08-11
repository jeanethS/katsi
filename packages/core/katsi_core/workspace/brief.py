"""Workspace Brief assembly from authoritative state and serialized byte budgets.

Reads the authoritative active intent, Claims, workspace records, open work,
active advisory leases, and recent workspace events from SQLite (through the
existing services and ``WorkspaceRepository``), then accounts for the actual
serialized byte cost of each candidate against a caller-supplied byte budget.
Overflow is omitted with explicit omission and provisional markers so a caller
can tell an agent exactly what was held back and why. Graph/vector context
fusion is added by a separate layer; this service never depends on projections
being current to represent durable state truthfully.
"""

from __future__ import annotations

from typing import TypeVar

from katsi_core.config import BriefSettings, ProjectionWorkerSettings
from katsi_core.store.projection_worker import ProjectionWorker
from katsi_core.store.workspace_repository import WorkspaceRepository
from katsi_core.store.workspace_sqlite import WorkspaceSQLite
from katsi_core.workspace.budget import BudgetItem, BudgetResult, SerializedBudgeter
from katsi_core.workspace.claims import ClaimService
from katsi_core.workspace.contracts import (
    BriefClaim,
    BriefLease,
    BriefOpenWork,
    BriefRecentEvent,
    BriefRecord,
    BriefSection,
    Claim,
    ClaimStatus,
    OmittedSection,
    OpenWork,
    OpenWorkStatus,
    ProjectionFreshness,
    WorkLease,
    WorkspaceBrief,
    WorkspaceEvent,
    WorkspaceId,
    WorkspaceRecord,
    WorkspaceRecordKind,
    WorkspaceRecordStatus,
)
from katsi_core.workspace.errors import WorkspaceError
from katsi_core.workspace.intent import IntentService
from katsi_core.workspace.leases import WorkLeaseService
from katsi_core.workspace.records import WorkspaceRecordService

_PRIORITY_GOAL = 0
_PRIORITY_DECISION_VERIFIED = 10
_PRIORITY_CLAIM_VERIFIED = 20
_PRIORITY_BLOCKER = 30
_PRIORITY_DECISION_OPEN = 35
_PRIORITY_CLAIM_CORROBORATED = 40
_PRIORITY_OPEN_QUESTION = 50
_PRIORITY_CLAIM_PROPOSED = 60
_PRIORITY_OPEN_WORK = 70
_PRIORITY_LEASE = 80
_PRIORITY_RECENT_EVENT = 90
_PRIORITY_CLAIM_INVALIDATED = 100

_APPLICABLE_CLAIM_STATUSES = frozenset(
    {ClaimStatus.PROPOSED, ClaimStatus.CORROBORATED, ClaimStatus.VERIFIED, ClaimStatus.INVALIDATED}
)
_ACTIVE_OPEN_WORK_STATUSES = frozenset({OpenWorkStatus.OPEN, OpenWorkStatus.BLOCKED})
_VISIBLE_DECISION_STATUSES = frozenset({WorkspaceRecordStatus.OPEN, WorkspaceRecordStatus.VERIFIED})

_BriefModelT = TypeVar("_BriefModelT")


class BriefService:
    """Assembles a budget-bounded brief from authoritative workspace state."""

    def __init__(
        self,
        repository: WorkspaceRepository,
        database: WorkspaceSQLite,
        intents: IntentService,
        claims: ClaimService,
        records: WorkspaceRecordService,
        leases: WorkLeaseService,
        settings: BriefSettings,
    ) -> None:
        self._repository = repository
        self._database = database
        self._intents = intents
        self._claims = claims
        self._records = records
        self._leases = leases
        self._settings = settings
        self._budgeter = SerializedBudgeter()

    def assemble(self, workspace_id: WorkspaceId, *, byte_budget: int) -> WorkspaceBrief:
        """Produce the most relevant durable state within the caller's byte budget."""
        if byte_budget < 0:
            raise ValueError("byte_budget must be non-negative")

        workspace = self._repository.get_workspace(workspace_id)
        if workspace is None:
            raise WorkspaceError(f"unknown workspace: {workspace_id}")
        state_version = workspace.state_version
        last_event_sequence = self._repository.last_event_sequence(workspace_id)
        projection_freshness = self._projection_freshness(workspace_id)
        projection_lag = any(entry.lagging for entry in projection_freshness)

        items: list[BudgetItem] = []
        claim_models: dict[str, BriefClaim] = {}
        decision_models: dict[str, BriefRecord] = {}
        blocker_models: dict[str, BriefRecord] = {}
        question_models: dict[str, BriefRecord] = {}
        work_models: dict[str, BriefOpenWork] = {}
        lease_models: dict[str, BriefLease] = {}
        event_models: dict[str, BriefRecentEvent] = {}

        goal_payload = self._intents.get(workspace_id)
        goal_key = f"goal:{workspace_id}"
        if goal_payload is not None:
            goal, version = goal_payload
            items.append(
                BudgetItem(
                    key=goal_key,
                    section=BriefSection.GOAL.value,
                    payload={"goal": goal, "version": version},
                    priority=_PRIORITY_GOAL,
                )
            )

        for claim in self._claims.list_for_workspace(workspace_id):
            if claim.status not in _APPLICABLE_CLAIM_STATUSES:
                continue
            key = f"claim:{claim.id}"
            brief_claim = self._brief_claim(claim)
            claim_models[key] = brief_claim
            items.append(
                BudgetItem(
                    key=key,
                    section=BriefSection.CLAIM.value,
                    payload=brief_claim.model_dump(mode="json"),
                    priority=self._claim_priority(claim.status),
                    provisional=claim.status is not ClaimStatus.VERIFIED,
                    metadata={"claim_id": str(claim.id), "status": claim.status.value},
                )
            )

        for record in self._records.list_records(workspace_id):
            key = f"record:{record.id}"
            brief_record = self._brief_record(record)
            if (
                record.kind is WorkspaceRecordKind.DECISION
                and record.status in _VISIBLE_DECISION_STATUSES
            ):
                decision_models[key] = brief_record
                items.append(
                    BudgetItem(
                        key=key,
                        section=BriefSection.DECISION.value,
                        payload=brief_record.model_dump(mode="json"),
                        priority=self._decision_priority(record.status),
                        provisional=record.status is not WorkspaceRecordStatus.VERIFIED,
                        metadata={"record_id": str(record.id), "status": record.status.value},
                    )
                )
            elif (
                record.kind is WorkspaceRecordKind.BLOCKER
                and record.status is WorkspaceRecordStatus.OPEN
            ):
                blocker_models[key] = brief_record
                items.append(
                    BudgetItem(
                        key=key,
                        section=BriefSection.BLOCKER.value,
                        payload=brief_record.model_dump(mode="json"),
                        priority=_PRIORITY_BLOCKER,
                        provisional=True,
                        metadata={"record_id": str(record.id)},
                    )
                )
            elif (
                record.kind is WorkspaceRecordKind.OPEN_QUESTION
                and record.status is WorkspaceRecordStatus.OPEN
            ):
                question_models[key] = brief_record
                items.append(
                    BudgetItem(
                        key=key,
                        section=BriefSection.OPEN_QUESTION.value,
                        payload=brief_record.model_dump(mode="json"),
                        priority=_PRIORITY_OPEN_QUESTION,
                        provisional=True,
                        metadata={"record_id": str(record.id)},
                    )
                )

        for work in self._records.list_open_work(workspace_id):
            if work.status not in _ACTIVE_OPEN_WORK_STATUSES:
                continue
            key = f"open_work:{work.id}"
            brief_work = self._brief_open_work(work)
            work_models[key] = brief_work
            items.append(
                BudgetItem(
                    key=key,
                    section=BriefSection.OPEN_WORK.value,
                    payload=brief_work.model_dump(mode="json"),
                    priority=_PRIORITY_OPEN_WORK,
                    provisional=True,
                    metadata={"open_work_id": str(work.id), "status": work.status.value},
                )
            )

        for lease in self._leases.active_for_workspace(workspace_id):
            key = f"lease:{lease.id}"
            brief_lease = self._brief_lease(lease)
            lease_models[key] = brief_lease
            items.append(
                BudgetItem(
                    key=key,
                    section=BriefSection.LEASE.value,
                    payload=brief_lease.model_dump(mode="json"),
                    priority=_PRIORITY_LEASE,
                    metadata={"lease_id": str(lease.id), "holder_id": str(lease.holder_id)},
                )
            )

        for event in self._recent_events(workspace_id):
            key = f"recent_event:{event.event_sequence}"
            event_models[key] = event
            items.append(
                BudgetItem(
                    key=key,
                    section=BriefSection.RECENT_EVENT.value,
                    payload=event.model_dump(mode="json"),
                    priority=_PRIORITY_RECENT_EVENT,
                    metadata={"event_sequence": str(event.event_sequence)},
                )
            )

        result = self._budgeter.fit(items, byte_budget)
        included = result.included_keys

        intent = goal_payload if goal_payload is not None and goal_key in included else None
        return WorkspaceBrief(
            workspace_id=workspace_id,
            state_version=state_version,
            last_event_sequence=last_event_sequence,
            intent=intent,
            claims=self._selected(claim_models, included),
            decisions=self._selected(decision_models, included),
            blockers=self._selected(blocker_models, included),
            open_questions=self._selected(question_models, included),
            open_work=self._selected(work_models, included),
            leases=self._selected(lease_models, included),
            recent_events=self._selected(event_models, included),
            projection_freshness=tuple(projection_freshness),
            budget_bytes=byte_budget,
            bytes_used=result.bytes_used,
            omitted=self._omitted_sections(result),
            provisional=self._provisional_sections(result),
            projection_lag=projection_lag,
        )

    def _recent_events(self, workspace_id: WorkspaceId) -> list[BriefRecentEvent]:
        events = self._repository.recent_events(
            workspace_id, limit=self._settings.recent_event_limit
        )
        return [self._brief_event(event) for event in events]

    def _projection_freshness(self, workspace_id: WorkspaceId) -> list[ProjectionFreshness]:
        return list(
            ProjectionWorker(self._database, ProjectionWorkerSettings()).freshness(workspace_id)
        )

    @staticmethod
    def _selected(
        models: dict[str, _BriefModelT], included: frozenset[str]
    ) -> tuple[_BriefModelT, ...]:
        return tuple(model for key, model in models.items() if key in included)

    @staticmethod
    def _omitted_sections(result: BudgetResult) -> tuple[OmittedSection, ...]:
        counts: dict[str, int] = {}
        for omitted in result.omitted:
            counts[omitted.section] = counts.get(omitted.section, 0) + 1
        return tuple(
            OmittedSection(section=BriefSection(section), count=count, reason="budget")
            for section, count in sorted(counts.items())
        )

    @staticmethod
    def _provisional_sections(result: BudgetResult) -> tuple[BriefSection, ...]:
        provisional: list[BriefSection] = []
        seen: set[str] = set()
        for item in result.included:
            if item.provisional and item.section not in seen:
                seen.add(item.section)
                provisional.append(BriefSection(item.section))
        return tuple(provisional)

    @staticmethod
    def _claim_priority(status: ClaimStatus) -> int:
        if status is ClaimStatus.VERIFIED:
            return _PRIORITY_CLAIM_VERIFIED
        if status is ClaimStatus.CORROBORATED:
            return _PRIORITY_CLAIM_CORROBORATED
        if status is ClaimStatus.INVALIDATED:
            return _PRIORITY_CLAIM_INVALIDATED
        return _PRIORITY_CLAIM_PROPOSED

    @staticmethod
    def _decision_priority(status: WorkspaceRecordStatus) -> int:
        if status is WorkspaceRecordStatus.VERIFIED:
            return _PRIORITY_DECISION_VERIFIED
        return _PRIORITY_DECISION_OPEN

    @staticmethod
    def _brief_claim(claim: Claim) -> BriefClaim:
        return BriefClaim(
            id=claim.id,
            text=claim.text,
            author_id=claim.author_id,
            status=claim.status,
            confidence=claim.confidence,
            scope_paths=claim.scope_paths,
            created_at=claim.created_at,
            invalidated=claim.status is ClaimStatus.INVALIDATED,
        )

    @staticmethod
    def _brief_record(record: WorkspaceRecord) -> BriefRecord:
        return BriefRecord(
            id=record.id,
            kind=record.kind,
            text=record.text,
            status=record.status,
            author_id=record.author_id,
            created_at=record.created_at,
        )

    @staticmethod
    def _brief_open_work(work: OpenWork) -> BriefOpenWork:
        return BriefOpenWork(
            id=work.id,
            description=work.description,
            status=work.status,
            author_id=work.author_id,
            created_at=work.created_at,
        )

    @staticmethod
    def _brief_lease(lease: WorkLease) -> BriefLease:
        return BriefLease(
            id=lease.id,
            holder_id=lease.holder_id,
            task_description=lease.task_description,
            resource_scope=lease.resource_scope,
            expires_at=lease.expires_at,
        )

    @staticmethod
    def _brief_event(event: WorkspaceEvent) -> BriefRecentEvent:
        detail = dict(event.detail)
        return BriefRecentEvent(
            event_sequence=event.sequence,
            kind=event.kind,
            occurred_at=event.occurred_at,
            path=detail.get("path"),
            correlation_id=event.correlation_id,
            detail=detail,
        )
