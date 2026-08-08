"""Serialized budget accounting for compact provenance-backed briefs.

Replaces fixed per-item token estimates with accounting over the actual
serialized byte cost of each candidate item. Each candidate carries its
structured payload; the budgeter serializes that payload to compact JSON and
accounts for the real UTF-8 byte length. Entries that do not fit a
caller-supplied byte budget are reported as explicit omissions so a caller can
tell an agent exactly what was held back and why.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class BudgetItem:
    """One candidate entry the budgeter may include in a brief.

    ``payload`` is the structured candidate (for example a dumped brief model).
    The budgeter serializes it to measure the real cost, so the accounted bytes
    match the form in which the item would be emitted. Items with equal
    ``priority`` preserve caller insertion order; lower priority numbers are
    included before higher ones.
    """

    key: str
    section: str
    payload: Mapping[str, object]
    priority: int
    provisional: bool = False
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BudgetedItem:
    """A ``BudgetItem`` that fit the budget, annotated with its real cost."""

    key: str
    section: str
    priority: int
    provisional: bool
    byte_count: int
    metadata: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class OmittedItem:
    """An entry held back from the brief with the reason and its would-be cost."""

    key: str
    section: str
    byte_count: int
    reason: str


@dataclass(frozen=True, slots=True)
class BudgetResult:
    included: tuple[BudgetedItem, ...]
    omitted: tuple[OmittedItem, ...]
    bytes_used: int
    budget_bytes: int

    @property
    def included_keys(self) -> frozenset[str]:
        return frozenset(item.key for item in self.included)

    @property
    def exhausted(self) -> bool:
        return self.bytes_used >= self.budget_bytes


class SerializedBudgeter:
    """Accounts for serialized content rather than fixed per-item estimates."""

    @staticmethod
    def serialized_bytes(payload: Mapping[str, object]) -> int:
        """Measure the UTF-8 byte length of the candidate's compact JSON form."""
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
        return len(encoded.encode("utf-8"))

    def fit(self, items: Sequence[BudgetItem], budget_bytes: int) -> BudgetResult:
        """Greedily fit serialized items into ``budget_bytes`` in priority order.

        Items that do not fit are skipped so smaller, lower-priority entries may
        still use the remaining budget; every skipped item is reported as an
        explicit omission with reason ``"budget"``.
        """
        if budget_bytes < 0:
            raise ValueError("budget_bytes must be non-negative")
        ordered = sorted(items, key=lambda item: item.priority)
        included: list[BudgetedItem] = []
        omitted: list[OmittedItem] = []
        bytes_used = 0
        for item in ordered:
            cost = self.serialized_bytes(item.payload)
            if cost <= budget_bytes - bytes_used:
                included.append(
                    BudgetedItem(
                        key=item.key,
                        section=item.section,
                        priority=item.priority,
                        provisional=item.provisional,
                        byte_count=cost,
                        metadata=item.metadata,
                    )
                )
                bytes_used += cost
            else:
                omitted.append(
                    OmittedItem(
                        key=item.key,
                        section=item.section,
                        byte_count=cost,
                        reason="budget",
                    )
                )
        return BudgetResult(tuple(included), tuple(omitted), bytes_used, budget_bytes)
