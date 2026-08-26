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


# --------------------------------------------------------------------------- #
# Dashboard: variants, stages, alerts
# --------------------------------------------------------------------------- #


def _req(**fields):
    return json.dumps({"event": "request_completed", **fields})


def _stage(**fields):
    return json.dumps({"event": "stage.completed", **fields})


def test_forced_requests_are_excluded_from_the_ab_comparison():
    """A demo request is not evidence about the population.

    Letting X-Variant calls into the comparison is how a demo ends up deciding a
    rollout.
    """
    from eval.dashboard import aggregate, parse_events

    events = parse_events([
        _req(path="/e", status=200, latency_ms=1000, cost_usd=0.02, variant="a"),
        _req(path="/e", status=200, latency_ms=1000, cost_usd=0.01, variant="b"),
        _req(path="/e", status=200, latency_ms=9000, cost_usd=9.99, variant="b",
             variant_forced=True),
    ])
    by_variant = aggregate(events)["by_variant"]
    assert by_variant["b"]["requests"] == 1
    assert by_variant["b"]["cost_mean_usd"] == pytest.approx(0.01)


def test_stage_costs_are_ranked_by_where_the_money_actually_goes():
    from eval.dashboard import aggregate, parse_events

    stages = parse_events([
        _stage(stage="retrieval", duration_ms=120, stage_cost_usd=0.0),
        _stage(stage="generation", duration_ms=90_000, stage_cost_usd=0.29),
        _stage(stage="reformulation", duration_ms=1_200, stage_cost_usd=0.01),
    ], "stage.completed")
    by_stage = aggregate([], stages)["by_stage"]
    assert list(by_stage) == ["generation", "reformulation", "retrieval"]
    assert by_stage["retrieval"]["cost_total_usd"] == 0.0


def test_the_abstention_rate_is_a_first_class_signal():
    """Read next to the error rate: a rise here is the system being careful, a
    rise there is the system being broken."""
    from eval.dashboard import aggregate, parse_events

    events = parse_events([
        _req(path="/e", status=200, latency_ms=10, abstained=True),
        _req(path="/e", status=200, latency_ms=10, abstained=False),
    ])
    assert aggregate(events)["overall"]["abstention_rate"] == 0.5


@pytest.mark.parametrize(
    "kwargs, expected_fragment",
    [
        ({"p95_ms": 500.0, "cost_usd": None, "error_rate": None}, "p95 latency"),
        ({"p95_ms": None, "cost_usd": 0.001, "error_rate": None}, "cost per request"),
        ({"p95_ms": None, "cost_usd": None, "error_rate": 0.1}, "error rate"),
    ],
)
def test_each_threshold_can_fire_on_its_own(kwargs, expected_fragment):
    from eval.dashboard import aggregate, check_alerts, parse_events

    report = aggregate(parse_events([
        _req(path="/e", status=200, latency_ms=2000, cost_usd=0.05),
        _req(path="/e", status=502, latency_ms=100, cost_usd=0.0),
    ]))
    breaches = check_alerts(report, **kwargs)
    assert any(expected_fragment in b for b in breaches)


def test_no_threshold_means_no_alert():
    """Absent is 'not watched', not 'watched with a default nobody chose'."""
    from eval.dashboard import aggregate, check_alerts, parse_events

    report = aggregate(parse_events([_req(path="/e", status=502, latency_ms=99_999)]))
    assert check_alerts(report, p95_ms=None, cost_usd=None, error_rate=None) == []


def test_a_report_that_does_not_declare_its_arm_is_never_matched(tmp_path):
    """Treating an unlabelled run as A publishes a conclusion about a run that
    never happened."""
    from eval.compare_variants import latest_for_variant

    (tmp_path / "report-1.json").write_text(json.dumps({"metrics": {}}))
    (tmp_path / "report-2.json").write_text(json.dumps({"variant": "b", "metrics": {}}))
    assert latest_for_variant(tmp_path, "a") is None
    assert latest_for_variant(tmp_path, "b").name == "report-2.json"
