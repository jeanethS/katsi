"""Tests for serialized byte budget accounting (OpenSpec task 10.5)."""

from __future__ import annotations

import pytest

from katsi_core.workspace.budget import BudgetItem, SerializedBudgeter


def test_serialized_bytes_measures_compact_json_utf8_length() -> None:
    budgeter = SerializedBudgeter()

    assert budgeter.serialized_bytes({}) == len(b"{}")
    # Compact JSON for {"k":"ab"} is exactly {"k":"ab"} (10 bytes).
    assert budgeter.serialized_bytes({"k": "ab"}) == 10
    # Keys are sorted and whitespace is removed, so key order never changes cost.
    assert budgeter.serialized_bytes({"b": 1, "a": 1}) == len(b'{"a":1,"b":1}')


def test_fit_includes_lower_priority_first_and_preserves_ties() -> None:
    budgeter = SerializedBudgeter()
    tiny = budgeter.serialized_bytes({"v": "b"})
    low_cost = budgeter.serialized_bytes({"v": "aaaa"})
    items = [
        BudgetItem(key="low", section="s", payload={"v": "aaaa"}, priority=10),
        BudgetItem(key="high-a", section="s", payload={"v": "b"}, priority=0),
        BudgetItem(key="high-b", section="s", payload={"v": "b"}, priority=0),
    ]
    result = budgeter.fit(items, budget_bytes=2 * tiny + low_cost + 1)

    assert [item.key for item in result.included] == ["high-a", "high-b", "low"]
    assert result.bytes_used == 2 * tiny + low_cost
    assert not result.exhausted
    assert result.omitted == ()


def test_fit_skips_oversized_item_and_fills_remaining_budget() -> None:
    budgeter = SerializedBudgeter()
    huge_payload = {"v": "x" * 200}
    huge_cost = budgeter.serialized_bytes(huge_payload)
    tiny_cost = budgeter.serialized_bytes({"v": "y"})
    items = [
        BudgetItem(key="huge", section="claim", payload=huge_payload, priority=0),
        BudgetItem(key="tiny", section="decision", payload={"v": "y"}, priority=10),
    ]
    result = budgeter.fit(items, budget_bytes=tiny_cost)

    assert [item.key for item in result.included] == ["tiny"]
    assert [item.key for item in result.omitted] == ["huge"]
    assert result.omitted[0].reason == "budget"
    assert result.omitted[0].byte_count == huge_cost
    assert result.bytes_used == tiny_cost


def test_fit_reports_omitted_counts_per_section() -> None:
    budgeter = SerializedBudgeter()
    claim_payload = {"v": "x" * 50}
    decision_payload = {"v": "y"}
    items = [
        BudgetItem(key="c1", section="claim", payload=claim_payload, priority=0),
        BudgetItem(key="c2", section="claim", payload=claim_payload, priority=1),
        BudgetItem(key="d1", section="decision", payload=decision_payload, priority=2),
        BudgetItem(key="d2", section="decision", payload=decision_payload, priority=3),
    ]
    two_decisions = 2 * budgeter.serialized_bytes(decision_payload)
    result = budgeter.fit(items, budget_bytes=two_decisions)

    assert {item.key for item in result.included} == {"d1", "d2"}
    sections = {om.section: om.byte_count for om in result.omitted}
    assert sections == {"claim": budgeter.serialized_bytes(claim_payload)}
    assert set(om.key for om in result.omitted) == {"c1", "c2"}


def test_fit_rejects_negative_budget() -> None:
    budgeter = SerializedBudgeter()
    with pytest.raises(ValueError):
        budgeter.fit(
            [BudgetItem(key="a", section="s", payload={"v": 1}, priority=0)],
            budget_bytes=-1,
        )


def test_fit_marks_provisional_flag_through_to_included_items() -> None:
    budgeter = SerializedBudgeter()
    items = [
        BudgetItem(
            key="verified", section="claim", payload={"v": "a"}, priority=0, provisional=False
        ),
        BudgetItem(
            key="proposed", section="claim", payload={"v": "b"}, priority=10, provisional=True
        ),
    ]
    result = budgeter.fit(items, budget_bytes=10_000)

    provisional = {item.key: item.provisional for item in result.included}
    assert provisional == {"verified": False, "proposed": True}
