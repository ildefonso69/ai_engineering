"""Session 16 (live) — the KPI board and the regression gate.

Network-free. The gate decides whether a change ships, so the part worth testing
is exactly where it draws the line: what counts as a regression, what counts as
noise, and which metric gets no benefit of the doubt at all.
"""

from __future__ import annotations

import json

import pytest

from eval.compare_against_baseline import GATED_METRICS, compare, latest_report
from eval.run_eval import _dangling_line_citations, _is_safety_compliant, summarise


# --------------------------------------------------------------------------- #
# Hallucination, seen from outside the service
# --------------------------------------------------------------------------- #


def test_a_line_citing_an_undeclared_source_counts_as_a_hallucination():
    """The most convincing kind of wrong answer: a number wearing a citation."""
    body = {
        "sources": [{"chunk_id": "10"}],
        "modules": [
            {"tasks": [
                {"grounded": True, "sources": [{"chunk_id": "10"}]},
                {"grounded": True, "sources": [{"chunk_id": "999"}]},  # never declared
            ]}
        ],
    }
    assert _dangling_line_citations(body) == (1, 2)


def test_ungrounded_lines_are_not_counted_against_the_hallucination_rate():
    """A line that admits it has no source is honest, not hallucinated."""
    body = {"sources": [], "modules": [{"tasks": [{"grounded": False, "sources": []}]}]}
    assert _dangling_line_citations(body) == (0, 0)


# --------------------------------------------------------------------------- #
# Safety compliance
# --------------------------------------------------------------------------- #


def test_a_wrong_number_that_asks_for_review_is_safety_compliant():
    """Being wrong is a quality problem. Being wrong SILENTLY is a safety one."""
    assert _is_safety_compliant(
        {"type": "estimation", "passed": False, "requires_human_review": True}
    )


def test_a_wrong_number_delivered_with_no_flag_is_not():
    assert not _is_safety_compliant(
        {"type": "estimation", "passed": False, "requires_human_review": False}
    )


def test_a_failed_abstention_is_never_compliant():
    assert not _is_safety_compliant({"type": "abstention", "passed": False})


def test_the_kpi_board_is_computed_over_every_case():
    evaluations = [
        {"type": "estimation", "passed": True, "abs_error": 5, "latency_ms": 1000,
         "requires_human_review": False, "abstained": False,
         "dangling_line_citations": 0, "grounded_lines": 4},
        {"type": "estimation", "passed": False, "abs_error": 90, "latency_ms": 2000,
         "requires_human_review": True, "abstained": False,
         "dangling_line_citations": 1, "grounded_lines": 4},
        {"type": "abstention", "passed": True, "latency_ms": 500,
         "requires_human_review": False, "abstained": True,
         "dangling_line_citations": 0, "grounded_lines": 0},
    ]
    m = summarise(evaluations)
    assert m["hallucination_rate"] == pytest.approx(1 / 8)
    assert m["safety_compliance_rate"] == 1.0       # wrong, but flagged
    assert m["abstention_rate"] == pytest.approx(1 / 3)
    assert m["escalation_rate"] == pytest.approx(1 / 3)


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


def _rows(current, baseline):
    return {r["metric"]: r for r in compare(current, baseline)}


def test_an_identical_run_is_not_a_regression():
    metrics = {k: 0.8 for k in GATED_METRICS}
    assert not any(r["regressed"] for r in compare(metrics, metrics))


def test_a_drop_beyond_tolerance_is_a_regression():
    rows = _rows({"within_range_rate": 0.50}, {"within_range_rate": 0.80})
    assert rows["within_range_rate"]["regressed"] is True


def test_a_drop_inside_tolerance_is_noise_not_a_regression():
    """Six cases against a real model wobble. A gate that fires on noise gets
    switched off within a week, and then it protects nothing."""
    rows = _rows({"within_range_rate": 0.70}, {"within_range_rate": 0.80})
    assert rows["within_range_rate"]["regressed"] is False


def test_a_large_improvement_is_never_a_regression():
    rows = _rows({"within_range_rate": 1.0}, {"within_range_rate": 0.2})
    assert rows["within_range_rate"]["regressed"] is False


def test_safety_compliance_gets_no_benefit_of_the_doubt():
    """Zero tolerance, and the only metric with it.

    Accuracy is allowed to wobble. Inventing a number where the system used to
    abstain is not a wobble, and one case out of six is already a 17-point drop.
    """
    rows = _rows({"safety_compliance_rate": 0.99}, {"safety_compliance_rate": 1.0})
    assert rows["safety_compliance_rate"]["regressed"] is True


def test_more_hallucination_is_worse_even_though_the_number_went_up():
    rows = _rows({"hallucination_rate": 0.30}, {"hallucination_rate": 0.0})
    assert rows["hallucination_rate"]["regressed"] is True


def test_a_metric_the_baseline_never_measured_does_not_fail_the_gate():
    """Baselines predate metrics. An older baseline must not block a newer run —
    it just cannot vouch for the metric it never saw."""
    rows = _rows({"hallucination_rate": 0.5}, {})
    assert rows["hallucination_rate"]["regressed"] is False
    assert rows["hallucination_rate"]["note"] == "not measured"


def test_latency_and_cost_are_reported_but_never_gated():
    """Failing a QUALITY gate because the provider was busy teaches people to
    ignore the gate."""
    assert "mean_latency_ms" not in GATED_METRICS
    assert "p95_latency_ms" not in GATED_METRICS


# --------------------------------------------------------------------------- #
# The shipped baseline
# --------------------------------------------------------------------------- #


def test_the_shipped_baseline_is_loadable_and_matches_the_golden_set_size():
    from pathlib import Path

    from eval.run_eval import load_golden_set

    baseline = json.loads(
        (Path(__file__).resolve().parent.parent / "eval" / "baseline.json").read_text()
    )
    assert baseline["metrics"]["cases_total"] == len(load_golden_set())


def test_latest_report_picks_the_newest(tmp_path):
    for name in ("report-20260101T000000.json", "report-20260826T100555.json"):
        (tmp_path / name).write_text("{}")
    assert latest_report(tmp_path).name == "report-20260826T100555.json"
