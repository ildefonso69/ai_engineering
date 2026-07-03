"""Unit tests for the Session 12 agent tools (no network, no DB)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.generation.agentic.agent_schemas import SearchBudgetsArgs
from app.generation.agentic.agent_tools import (
    CONTINGENCY_FACTOR,
    calculate_estimate,
    dispatch_tool,
    search_budgets,
    validate_estimate,
)


# --- calculate_estimate ---------------------------------------------------- #
def test_calculate_estimate_median_plus_contingency():
    result = calculate_estimate(
        {"components": [{"name": "Auth backend", "reference_amounts": [100.0, 200.0, 300.0]}]}
    )
    # median(100,200,300)=200; +15% contingency => 230.
    assert result["components"][0]["estimated_hours"] == pytest.approx(
        200 * (1 + CONTINGENCY_FACTOR)
    )
    assert result["total_hours"] == pytest.approx(230.0)
    assert result["components"][0]["unbudgeted"] is False


def test_calculate_estimate_flags_unbudgeted_without_inventing_hours():
    result = calculate_estimate(
        {"components": [{"name": "Mystery module", "reference_amounts": []}]}
    )
    component = result["components"][0]
    assert component["estimated_hours"] == 0.0
    assert component["unbudgeted"] is True
    assert result["total_hours"] == 0.0


def test_calculate_estimate_sums_components():
    result = calculate_estimate(
        {
            "components": [
                {"name": "A", "reference_amounts": [100.0]},
                {"name": "B", "reference_amounts": [200.0]},
            ]
        }
    )
    assert result["total_hours"] == pytest.approx(115.0 + 230.0)


def test_calculate_estimate_rejects_bad_args():
    with pytest.raises(ValidationError):
        calculate_estimate({"components": [{"name": "A"}]})  # missing reference_amounts


# --- validate_estimate ----------------------------------------------------- #
def test_validate_estimate_passes_clean_estimate():
    result = validate_estimate(
        {
            "components": [{"name": "A", "estimated_hours": 115.0, "reference_amounts": [100.0]}],
            "total_hours": 115.0,
        }
    )
    assert result["ok"] is True
    assert result["issues"] == []


def test_validate_estimate_flags_unbudgeted_and_total_mismatch():
    result = validate_estimate(
        {
            "components": [{"name": "A", "estimated_hours": 50.0, "reference_amounts": []}],
            "total_hours": 999.0,
        }
    )
    assert result["ok"] is False
    joined = " ".join(result["issues"]).lower()
    assert "no historical reference" in joined
    assert "does not match" in joined


def test_validate_estimate_flags_out_of_range_component():
    # reference 100 → plausible range [50, 200]; 1000 is out of range.
    result = validate_estimate(
        {
            "components": [{"name": "A", "estimated_hours": 1000.0, "reference_amounts": [100.0]}],
            "total_hours": 1000.0,
        }
    )
    assert result["ok"] is False
    assert any("outside the plausible range" in issue for issue in result["issues"])


def test_validate_estimate_flags_nonpositive_total():
    result = validate_estimate({"components": [], "total_hours": 0.0})
    assert result["ok"] is False
    assert any("non-positive" in issue for issue in result["issues"])


# --- search_budgets + dispatch --------------------------------------------- #
async def test_search_budgets_uses_injected_backend():
    async def fake_backend(args: SearchBudgetsArgs) -> list[dict]:
        assert args.query == "auth backend"
        return [{"id": 1, "estimated_hours": 420.0, "content_preview": "x", "distance": 0.1}]

    result = await search_budgets({"query": "auth backend", "filters": None}, backend=fake_backend)
    assert result["count"] == 1
    assert "420.0" in result["summary"]


async def test_search_budgets_rejects_empty_query():
    async def fake_backend(args: SearchBudgetsArgs) -> list[dict]:
        return []

    with pytest.raises(ValidationError):
        await search_budgets({"query": "", "filters": None}, backend=fake_backend)


async def test_dispatch_unknown_tool_raises():
    async def fake_backend(args: SearchBudgetsArgs) -> list[dict]:
        return []

    with pytest.raises(ValueError, match="Unknown tool"):
        await dispatch_tool("nonexistent", {}, backend=fake_backend)
