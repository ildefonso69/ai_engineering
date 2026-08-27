"""Session 16 — the output guardrail inside the graph flow.

The graph derives its hours from the historical corpus, so a fabricated total is
not the failure mode here. What these pin is the OTHER two: an estimate that
leans on almost no distinct evidence, and one where most tasks never found an
analog at all. Network-free: it is arithmetic over dicts.
"""

from __future__ import annotations

from app.config import get_settings
from app.domain.graph.agents._common import (
    build_estimate,
    evidence_hours_from_task_hours,
    recompute_estimate_totals,
    review_fields,
)


def _row(module: str, task: str, hours: int | None, neighbors: list[tuple[int, int]]) -> dict:
    return {
        "module": module,
        "task": task,
        "estimated_hours": hours,
        "reliability": None if hours is None else 0.8,
        "has_match": hours is not None,
        "neighbors": [
            {"source_id": sid, "estimated_hours": h, "distance": 0.2} for sid, h in neighbors
        ],
    }


def _approved(module: str, *tasks: str) -> list[dict]:
    return [{"name": module, "tasks": [{"name": t, "description": None} for t in tasks]}]


def test_evidence_is_counted_once_per_source_across_tasks():
    rows = [
        _row("M", "a", 40, [(7, 150)]),
        _row("M", "b", 32, [(7, 150), (9, 40)]),
    ]
    assert sorted(evidence_hours_from_task_hours(rows)) == [40, 150]


def test_a_healthy_estimate_carries_the_fields_and_no_reasons():
    """The two fields are always present, so the platform never has to ask."""
    rows = [_row("M", "a", 40, [(1, 150)]), _row("M", "b", 32, [(2, 110)])]
    estimate = build_estimate(_approved("M", "a", "b"), rows)

    assert estimate["requires_human_review"] is False
    assert estimate["review_reasons"] == []


def test_many_tasks_over_two_analogs_is_flagged():
    tasks = [f"task {i}" for i in range(40)]
    rows = [_row("M", t, 40, [(1, 150), (2, 110)]) for t in tasks]
    estimate = build_estimate(_approved("M", *tasks), rows)

    assert estimate["requires_human_review"] is True
    assert any("distinct historical analogs" in r for r in estimate["review_reasons"])


def test_mostly_ungrounded_tasks_are_flagged_as_such_not_as_a_zero_total():
    rows = [_row("M", "a", 40, [(1, 40)])] + [_row("M", f"u{i}", None, []) for i in range(3)]
    estimate = build_estimate(_approved("M", "a", "u0", "u1", "u2"), rows)

    assert estimate["requires_human_review"] is True
    assert estimate["review_reasons"] == ["3 of 4 tasks have no hours behind them"]


def test_a_human_filling_the_gaps_at_gate_two_clears_the_flag():
    """The reason the verdict is re-derived after the gate rather than carried.

    A stale "needs review" on an estimate the reviewer already fixed is how a
    team learns to click past the banner.
    """
    rows = [_row("M", "a", 40, [(1, 40)])] + [_row("M", f"u{i}", None, []) for i in range(3)]
    estimate = build_estimate(_approved("M", "a", "u0", "u1", "u2"), rows)
    assert estimate["requires_human_review"] is True

    for module in estimate["modules"]:
        for task in module["tasks"]:
            if task["estimated_hours"] is None:
                task["estimated_hours"] = 16

    totals = recompute_estimate_totals(estimate["modules"])
    assert review_fields(estimate["modules"], rows, totals)["requires_human_review"] is False


def test_the_guardrail_can_be_switched_off_by_configuration(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ESTIMATE_BOUNDS_ENABLED", False)
    rows = [_row("M", f"u{i}", None, []) for i in range(3)]
    modules = _approved("M", "u0", "u1", "u2")

    fields = review_fields(modules, rows, recompute_estimate_totals(modules), settings=settings)
    assert fields == {"requires_human_review": False, "review_reasons": []}
