"""Representation DAG planner and stage execution orchestration.

This module covers three related concerns from the multimedia understanding
design (Decision 10, and tasks.md Section 4):

1. **DAG planning** (:class:`PipelineDAGPlanner`): given declared stage
   input/output representation kinds, compute a topologically ordered plan
   of independent "waves" so that stages with no data dependency on each
   other can run concurrently and so that one stage's failure never blocks
   an unrelated sibling stage.
2. **Stage execution** (:class:`StageRunner`): per-stage idempotency (cache
   check before doing work), bounded retry, cooperative cancellation, and
   coverage aggregation that never reports a resource as fully understood
   when any contributing stage is partial, failed, or skipped.
3. **Concurrency limits** (:class:`ConcurrencyLimiter`): configured global
   and per-workspace concurrency caps, independently tracked for CPU, GPU,
   and generic media jobs so a GPU-bound transcription stage cannot starve
   cheap CPU-bound extraction stages (or vice versa).

``katsi_core.media.contracts.MediaPipelineDefinition`` now declares
``stage`` and ``input_kinds`` directly, reconciling the gap this module
previously worked around. :class:`PipelineNodeSpec` remains the DAG-facing
structural type (it also carries ``independent``, which is a planning-only
concern, not a pipeline-authoring concern), but
:func:`node_spec_from_pipeline_definition` builds one from a real
``MediaPipelineDefinition`` so callers no longer need to hand-construct
``PipelineNodeSpec`` in parallel with their pipeline registrations.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum

from katsi_core.media.cache import CacheLookupResult, RepresentationCache
from katsi_core.media.contracts import (
    DerivedRepresentation,
    MediaCoverage,
    MediaPipelineDefinition,
    MediaRepresentationKind,
    MediaRepresentationStatus,
    PipelineFingerprint,
    PipelineStage,
    ResourceVersionId,
)

# =============================================================================
# DAG planning
# =============================================================================


@dataclass(frozen=True, slots=True)
class PipelineNodeSpec:
    """Declared input/output contract for one DAG node (pipeline stage).

    ``input_kinds`` is empty for root stages that consume the raw source
    (e.g. ``detect``, ``extract_metadata``). Non-empty ``input_kinds`` means
    the stage consumes one or more upstream representation kinds and cannot
    run until at least one of them is available.

    ``independent`` marks whether this stage's failure is allowed to be
    isolated from sibling stages that share the same input -- i.e. whether
    it can run in a wave alongside stages it does not depend on without a
    shared failure domain. Nearly all stages should set this ``True``;
    setting it ``False`` documents a stage whose failure should also halt
    otherwise-independent siblings (rare, e.g. a shared precondition check).
    """

    stage: PipelineStage
    output_kind: MediaRepresentationKind
    input_kinds: frozenset[MediaRepresentationKind] = field(default_factory=frozenset)
    independent: bool = True
    pipeline_id: str = ""


def node_spec_from_pipeline_definition(
    definition: MediaPipelineDefinition,
    *,
    independent: bool = True,
) -> tuple[PipelineNodeSpec, ...]:
    """Build DAG node specs from a registered ``MediaPipelineDefinition``.

    A pipeline definition may declare more than one produced representation
    kind; the DAG planner reasons about one output kind per node, so this
    returns one :class:`PipelineNodeSpec` per kind in
    ``representation_kinds_produced``, all sharing the definition's
    ``stage``, ``input_kinds``, and ``id``.
    """
    input_kinds = frozenset(definition.input_kinds)
    return tuple(
        PipelineNodeSpec(
            stage=definition.stage,
            output_kind=output_kind,
            input_kinds=input_kinds,
            independent=independent,
            pipeline_id=definition.id,
        )
        for output_kind in definition.representation_kinds_produced
    )


class DAGCycleError(ValueError):
    """Raised when registered pipeline nodes form a cycle."""


class PipelineDAGPlanner:
    """Plans representation DAG execution waves from declared node specs."""

    def __init__(self, nodes: Iterable[PipelineNodeSpec] = ()) -> None:
        self._nodes: list[PipelineNodeSpec] = list(nodes)

    def register(self, node: PipelineNodeSpec) -> None:
        self._nodes.append(node)

    def nodes(self) -> tuple[PipelineNodeSpec, ...]:
        return tuple(self._nodes)

    def plan(
        self, available_input_kinds: frozenset[MediaRepresentationKind] = frozenset()
    ) -> list[list[PipelineNodeSpec]]:
        """Compute topologically ordered execution waves.

        Each wave is a list of nodes that are mutually independent (no node
        in a wave depends on the output of another node in the same wave)
        and can therefore be executed concurrently. A node becomes eligible
        for a wave once all of its declared ``input_kinds`` are satisfied by
        ``available_input_kinds`` or by the output of a prior wave.

        Raises:
            DAGCycleError: if remaining nodes can never become eligible
                (a cycle, or a dependency on a kind no node produces and
                that is not present in ``available_input_kinds``).
        """
        remaining = list(self._nodes)
        satisfied = set(available_input_kinds)
        waves: list[list[PipelineNodeSpec]] = []

        while remaining:
            eligible = [n for n in remaining if n.input_kinds <= satisfied]
            if not eligible:
                stuck = ", ".join(n.stage.value for n in remaining)
                raise DAGCycleError(
                    f"Cannot schedule remaining pipeline stages (cycle or missing "
                    f"input kind): {stuck}"
                )
            waves.append(eligible)
            for n in eligible:
                satisfied.add(n.output_kind)
                remaining.remove(n)

        return waves

    def downstream_of(self, kind: MediaRepresentationKind) -> tuple[PipelineNodeSpec, ...]:
        """Nodes that declare ``kind`` as one of their required inputs."""
        return tuple(n for n in self._nodes if kind in n.input_kinds)


# =============================================================================
# Concurrency limits
# =============================================================================


class JobResourceClass(StrEnum):
    """Resource categories that concurrency limits are tracked per."""

    CPU = "cpu"
    GPU = "gpu"
    MEDIA = "media"


@dataclass(frozen=True, slots=True)
class ConcurrencyLimits:
    """Global and per-workspace concurrency caps, per job resource class.

    Defaults to the same value for every resource class (as configured by
    ``MediaProcessingConfig.global_max_concurrent_jobs`` /
    ``workspace_max_concurrent_jobs``); pass explicit per-class overrides
    (e.g. a tighter GPU cap) when the deployment needs finer control.
    """

    global_limits: dict[JobResourceClass, int]
    workspace_limits: dict[JobResourceClass, int]

    @classmethod
    def uniform(cls, global_max: int, workspace_max: int) -> ConcurrencyLimits:
        return cls(
            global_limits=dict.fromkeys(JobResourceClass, global_max),
            workspace_limits=dict.fromkeys(JobResourceClass, workspace_max),
        )


class ConcurrencyLimitError(RuntimeError):
    """Raised by non-blocking acquire attempts when no slot is available."""


class ConcurrencyLimiter:
    """Enforces configured global and per-workspace concurrency limits.

    Two semaphores gate every job: a global one for its resource class, and
    a per-(workspace, resource class) one. A job only proceeds once both are
    acquired, so a single busy workspace cannot exceed its share of the
    global budget, and no workspace combination can exceed the global cap.
    """

    def __init__(self, limits: ConcurrencyLimits) -> None:
        self._limits = limits
        self._global_semaphores = {
            resource_class: threading.Semaphore(max_count)
            for resource_class, max_count in limits.global_limits.items()
        }
        self._workspace_semaphores: dict[tuple[str, JobResourceClass], threading.Semaphore] = {}
        self._lock = threading.Lock()

    def _workspace_semaphore(
        self, workspace_id: str, resource_class: JobResourceClass
    ) -> threading.Semaphore:
        key = (workspace_id, resource_class)
        with self._lock:
            sem = self._workspace_semaphores.get(key)
            if sem is None:
                max_count = self._limits.workspace_limits.get(resource_class, 1)
                sem = threading.Semaphore(max_count)
                self._workspace_semaphores[key] = sem
            return sem

    @contextmanager
    def acquire(
        self,
        workspace_id: str,
        resource_class: JobResourceClass = JobResourceClass.CPU,
        blocking: bool = True,
        timeout: float | None = None,
    ):
        """Context manager acquiring both the global and workspace slot.

        Raises:
            ConcurrencyLimitError: if ``blocking`` is ``False`` (or a
                ``timeout`` elapses) and no slot is currently available.
        """
        global_sem = self._global_semaphores[resource_class]
        workspace_sem = self._workspace_semaphore(workspace_id, resource_class)

        if not global_sem.acquire(blocking=blocking, timeout=timeout):
            raise ConcurrencyLimitError(
                f"Global concurrency limit reached for {resource_class.value}"
            )
        if not workspace_sem.acquire(blocking=blocking, timeout=timeout):
            global_sem.release()
            raise ConcurrencyLimitError(
                f"Workspace concurrency limit reached for {resource_class.value} "
                f"in workspace {workspace_id}"
            )
        try:
            yield
        finally:
            workspace_sem.release()
            global_sem.release()


# =============================================================================
# Stage execution: idempotency, retry, cancellation, coverage aggregation
# =============================================================================


class CancellationToken:
    """Cooperative cancellation signal shared across stage executions."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()


class StageOutcomeStatus(StrEnum):
    CACHE_HIT = "cache_hit"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """Result of attempting to satisfy one DAG node's representation."""

    node: PipelineNodeSpec
    status: StageOutcomeStatus
    representation: DerivedRepresentation | None = None
    error: str | None = None
    attempts: int = 0


class StageRunner:
    """Runs planned stages with idempotency, retry, and cancellation.

    Idempotency: before running a stage's work function, the runner checks
    the compatible cache (:class:`RepresentationCache`). A cache hit means
    the stage is never re-executed for content Katsi has already processed
    successfully under the same fingerprint -- this is what makes retries
    and re-planning of the same resource safe to repeat.

    Retry: transient failures (the work function raising) are retried up to
    ``max_retries`` additional times, with the cancellation token checked
    before every attempt so a cancelled run does not burn additional
    attempts.

    Sibling isolation: :meth:`run_wave` executes every node in a wave and
    catches failures per node, so one stage's failure/cancellation never
    prevents unrelated sibling stages in the same wave from completing.
    """

    def __init__(self, cache: RepresentationCache, max_retries: int = 1) -> None:
        self._cache = cache
        self._max_retries = max_retries

    def run_stage(
        self,
        node: PipelineNodeSpec,
        resource_version_id: ResourceVersionId,
        fingerprint: PipelineFingerprint,
        work: Callable[[], DerivedRepresentation],
        cancellation_token: CancellationToken | None = None,
    ) -> StageOutcome:
        token = cancellation_token or CancellationToken()

        cached = self._cache.find_compatible(resource_version_id, node.output_kind, fingerprint)
        if cached is not None:
            representation = self._cache.reuse_for_resource(resource_version_id, cached)
            status = (
                StageOutcomeStatus.CACHE_HIT
                if self._status_for(representation) == StageOutcomeStatus.SUCCESS
                else self._status_for(representation)
            )
            return StageOutcome(
                node=node,
                status=status,
                representation=representation,
                attempts=0,
            )

        if token.is_cancelled:
            return StageOutcome(node=node, status=StageOutcomeStatus.CANCELLED, attempts=0)

        last_error: str | None = None
        for attempt in range(1, self._max_retries + 2):
            if token.is_cancelled:
                return StageOutcome(
                    node=node, status=StageOutcomeStatus.CANCELLED, attempts=attempt - 1
                )
            try:
                representation = work()
            except Exception as exc:  # noqa: BLE001 - retried and surfaced as outcome
                last_error = str(exc)
                continue
            return StageOutcome(
                node=node,
                status=self._status_for(representation),
                representation=representation,
                attempts=attempt,
            )

        return StageOutcome(
            node=node,
            status=StageOutcomeStatus.FAILED,
            error=last_error,
            attempts=self._max_retries + 1,
        )

    @staticmethod
    def _status_for(representation: DerivedRepresentation) -> StageOutcomeStatus:
        if representation.status == MediaRepresentationStatus.PARTIAL:
            return StageOutcomeStatus.PARTIAL
        if representation.status in {
            MediaRepresentationStatus.FAILED,
            MediaRepresentationStatus.UNAVAILABLE,
        }:
            return StageOutcomeStatus.FAILED
        return StageOutcomeStatus.SUCCESS

    def run_wave(
        self,
        wave: Iterable[PipelineNodeSpec],
        resource_version_id: ResourceVersionId,
        fingerprint_for: Callable[[PipelineNodeSpec], PipelineFingerprint],
        work_for: Callable[[PipelineNodeSpec], Callable[[], DerivedRepresentation]],
        cancellation_token: CancellationToken | None = None,
    ) -> list[StageOutcome]:
        """Run every node in a wave, isolating each node's failure from its siblings."""
        outcomes: list[StageOutcome] = []
        for node in wave:
            try:
                outcome = self.run_stage(
                    node,
                    resource_version_id,
                    fingerprint_for(node),
                    work_for(node),
                    cancellation_token,
                )
            except Exception as exc:  # noqa: BLE001 - node isolation boundary
                outcome = StageOutcome(node=node, status=StageOutcomeStatus.FAILED, error=str(exc))
            outcomes.append(outcome)
        return outcomes


def aggregate_coverage(outcomes: Iterable[StageOutcome]) -> MediaCoverage:
    """Aggregate coverage across stage outcomes without overclaiming.

    A resource is reported fully understood (``is_complete=True``,
    ``coverage_fraction=1.0``) only when *every* stage outcome succeeded
    completely. Any partial, failed, or cancelled stage caps the aggregate
    at partial coverage -- a resource with 3 of 4 stages fully successful
    and one failed stage is never reported as 100% understood.
    """
    outcomes = list(outcomes)
    if not outcomes:
        return MediaCoverage(is_complete=False, coverage_fraction=0.0, detail="no stages planned")

    total = len(outcomes)
    fully_succeeded = sum(1 for o in outcomes if o.status == StageOutcomeStatus.SUCCESS)
    cache_hits_complete = sum(
        1
        for o in outcomes
        if o.status == StageOutcomeStatus.CACHE_HIT
        and o.representation is not None
        and o.representation.coverage.is_complete
    )
    complete_count = fully_succeeded + cache_hits_complete

    if complete_count == total:
        return MediaCoverage(is_complete=True, coverage_fraction=1.0, detail="all stages complete")

    failed = [o.node.stage.value for o in outcomes if o.status == StageOutcomeStatus.FAILED]
    cancelled = [o.node.stage.value for o in outcomes if o.status == StageOutcomeStatus.CANCELLED]
    partial = [o.node.stage.value for o in outcomes if o.status == StageOutcomeStatus.PARTIAL]

    detail_parts = []
    if failed:
        detail_parts.append(f"failed: {', '.join(failed)}")
    if cancelled:
        detail_parts.append(f"cancelled: {', '.join(cancelled)}")
    if partial:
        detail_parts.append(f"partial: {', '.join(partial)}")

    return MediaCoverage(
        is_complete=False,
        coverage_fraction=round(complete_count / total, 4),
        detail="; ".join(detail_parts) or "incomplete",
    )


__all__ = [
    "CacheLookupResult",
    "CancellationToken",
    "ConcurrencyLimitError",
    "ConcurrencyLimiter",
    "ConcurrencyLimits",
    "DAGCycleError",
    "JobResourceClass",
    "PipelineDAGPlanner",
    "PipelineNodeSpec",
    "StageOutcome",
    "StageOutcomeStatus",
    "StageRunner",
    "aggregate_coverage",
]
