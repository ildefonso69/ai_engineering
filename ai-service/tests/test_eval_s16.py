"""Session 16 — the evaluation harness, the dashboard and the cost accumulator.

Network-free, like every other test in this suite. The harness calls a real
model when a human runs it; here only its arithmetic and its refusals are under
test — which is the part that decides whether a report can be believed.
"""

from __future__ import annotations

import json

import pytest

from eval.dashboard import aggregate, parse_events, percentile, render_html
from eval.run_eval import evaluate_case, load_golden_set, summarise
from app.foundation.observability.metrics import (
    begin_request,
    current_metrics,
    end_request,
    record_llm_call,
)


# --------------------------------------------------------------------------- #
# The golden set itself
# --------------------------------------------------------------------------- #


def test_the_shipped_golden_set_is_valid_and_has_an_abstention_case():
    cases = load_golden_set()
    assert len(cases) >= 5
    abstentions = [c for c in cases if c["expect_abstention"]]
    assert len(abstentions) >= 1, (
        "without an abstention case the report measures accuracy but never safety"
    )
    # Every estimation case must carry an expectation that can actually fail.
    for case in cases:
        if not case["expect_abstention"]:
            low, high = case["acceptable_range"]
            assert low <= case["expected_engineer_days"] <= high


def test_load_golden_set_refuses_a_set_with_no_abstention_case(tmp_path):
    path = tmp_path / "g.json"
    path.write_text(
        json.dumps(
            [{"id": "a", "transcript": "x" * 120, "acceptable_range": [1, 2],
              "expected_engineer_days": 1, "expect_abstention": False}]
        )
    )
    with pytest.raises(ValueError, match="abstention"):
        load_golden_set(path)


@pytest.mark.parametrize(
    "case, expected_error",
    [
        ({"id": "a", "transcript": "too short", "acceptable_range": [1, 2]}, "100 characters"),
        ({"id": "a", "transcript": "x" * 120, "acceptable_range": [9, 2]}, "acceptable_range"),
        ({"id": None, "transcript": "x" * 120, "acceptable_range": [1, 2]}, "case id"),
    ],
)
def test_load_golden_set_refuses_malformed_cases(tmp_path, case, expected_error):
    path = tmp_path / "g.json"
    path.write_text(json.dumps([case]))
    with pytest.raises(ValueError, match=expected_error):
        load_golden_set(path)


# --------------------------------------------------------------------------- #
# Scoring one case
# --------------------------------------------------------------------------- #


def _result(body, status=200, latency_ms=1200.0):
    return {"status": status, "body": body, "latency_ms": latency_ms}


ESTIMATION_CASE = {
    "id": "c1",
    "difficulty": "easy",
    "acceptable_range": [45, 120],
    "expected_engineer_days": 75,
    "expect_abstention": False,
}
ABSTENTION_CASE = {"id": "c2", "difficulty": "abstention", "expect_abstention": True}


def test_an_estimate_inside_the_range_passes_even_when_it_is_not_the_expected_number():
    """The point of a range: an estimate is an interval, not a fact."""
    ev = evaluate_case(ESTIMATION_CASE, _result({"total_engineer_days": 110, "confidence": "medium"}))
    assert ev["passed"] is True
    assert ev["abs_error"] == 35


def test_an_estimate_outside_the_range_fails():
    ev = evaluate_case(ESTIMATION_CASE, _result({"total_engineer_days": 400, "confidence": "high"}))
    assert ev["passed"] is False
    assert "outside" in ev["detail"]


def test_abstaining_when_there_is_no_precedent_passes():
    ev = evaluate_case(
        ABSTENTION_CASE,
        _result({"total_engineer_days": None, "confidence": "insufficient"}),
    )
    assert ev["passed"] is True


def test_inventing_a_number_with_no_precedent_fails():
    """The dangerous failure: a confident answer where the honest one is 'I don't know'."""
    ev = evaluate_case(
        ABSTENTION_CASE,
        _result({"total_engineer_days": 260, "confidence": "high"}),
    )
    assert ev["passed"] is False
    assert "260" in ev["detail"]


def test_a_low_confidence_number_is_not_an_abstention():
    """'low' still hands the client a figure. Only 'insufficient' declines."""
    ev = evaluate_case(ABSTENTION_CASE, _result({"total_engineer_days": 90, "confidence": "low"}))
    assert ev["passed"] is False


def test_a_non_200_is_a_failed_case_not_a_missing_one():
    ev = evaluate_case(ESTIMATION_CASE, _result({"detail": "boom"}, status=502))
    assert ev["passed"] is False
    assert ev["type"] == "error"


def test_a_grounded_line_without_sources_breaks_citation_validity():
    body = {
        "total_engineer_days": 80,
        "confidence": "high",
        "modules": [{"tasks": [{"grounded": True, "sources": []}]}],
    }
    assert evaluate_case(ESTIMATION_CASE, _result(body))["citation_validity"] is False


# --------------------------------------------------------------------------- #
# Aggregating the report
# --------------------------------------------------------------------------- #


def test_summarise_computes_the_four_headline_metrics():
    evaluations = [
        {"type": "estimation", "passed": True, "abs_error": 10, "latency_ms": 1000},
        {"type": "estimation", "passed": False, "abs_error": 50, "latency_ms": 3000},
        {"type": "abstention", "passed": True, "latency_ms": 2000},
    ]
    m = summarise(evaluations)
    assert m["within_range_rate"] == 0.5
    assert m["mean_absolute_error"] == 30
    assert m["abstention_correct"] is True
    assert m["mean_latency_ms"] == 2000
    assert m["cases_passed"] == 2


def test_abstention_correct_is_none_rather_than_true_when_nothing_was_measured():
    """``all([])`` is True; reporting an unmeasured safety property as satisfied
    is exactly the kind of green light this exercise exists to prevent."""
    m = summarise([{"type": "estimation", "passed": True, "abs_error": 1, "latency_ms": 10}])
    assert m["abstention_correct"] is None


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #


def _log_line(**fields):
    return json.dumps({"event": "request_completed", **fields})


def test_parse_events_keeps_only_request_completed_and_survives_noise():
    lines = [
        "INFO:     Application startup complete.",
        json.dumps({"event": "llm_call_completed", "cost_usd": 0.1}),
        _log_line(path="/v1/estimate/from-transcript", status=200, latency_ms=1000, cost_usd=0.02),
        "not json at all {",
        "ai-service  | " + _log_line(path="/search", status=200, latency_ms=50, cost_usd=0.0),
        "",
    ]
    events = parse_events(lines)
    assert len(events) == 2
    assert {e["path"] for e in events} == {"/v1/estimate/from-transcript", "/search"}


def test_aggregate_reports_error_rate_latency_and_cost():
    events = parse_events(
        [
            _log_line(path="/e", status=200, latency_ms=1000, cost_usd=0.02, total_tokens=100, llm_calls=1),
            _log_line(path="/e", status=200, latency_ms=3000, cost_usd=0.04, total_tokens=300, llm_calls=2),
            _log_line(path="/e", status=502, latency_ms=200, cost_usd=0.0, total_tokens=0, llm_calls=0),
        ]
    )
    overall = aggregate(events)["overall"]
    assert overall["requests"] == 3
    assert overall["errors"] == 1
    assert overall["error_rate"] == pytest.approx(1 / 3)
    assert overall["cost_total_usd"] == pytest.approx(0.06)
    assert overall["cost_mean_usd"] == pytest.approx(0.02)
    assert overall["tokens_total"] == 400


def test_percentile_works_on_a_single_sample():
    assert percentile([1200.0], 95) == 1200.0
    assert percentile([], 95) is None


def test_render_html_is_self_contained():
    report = aggregate(parse_events([_log_line(path="/e", status=200, latency_ms=1000, cost_usd=0.02)]))
    page = render_html(report)
    assert "<!doctype html>" in page
    # A strict CSP is the deployment reality for anything published; keeping the
    # page dependency-free means it renders anywhere, including from a file://.
    assert "http://" not in page.replace('lang="en"', "")
    assert "<script" not in page


def test_dashboard_says_so_when_there_is_nothing_to_show():
    from eval.dashboard import render_terminal

    assert "No `request_completed`" in render_terminal(aggregate([]))


# --------------------------------------------------------------------------- #
# The per-request accumulator
# --------------------------------------------------------------------------- #


def test_the_accumulator_sums_every_llm_call_of_a_request():
    token = begin_request()
    record_llm_call(tokens_in=1000, tokens_out=200, cost_usd=0.001)
    record_llm_call(tokens_in=500, tokens_out=100, cost_usd=0.0005)
    metrics = end_request(token)

    assert metrics.as_dict() == {
        "llm_calls": 2,
        "prompt_tokens": 1500,
        "completion_tokens": 300,
        "total_tokens": 1800,
        "cost_usd": 0.0015,
    }


def test_recording_outside_a_request_is_a_no_op():
    """CLI scripts and the eval harness call the same code paths. Instrumentation
    must never be the reason something fails."""
    assert current_metrics() is None
    record_llm_call(tokens_in=10, tokens_out=10, cost_usd=1.0)  # must not raise


def test_cost_keeps_enough_decimals_to_not_round_cheap_calls_to_free():
    token = begin_request()
    record_llm_call(tokens_in=100, tokens_out=10, cost_usd=0.000021)
    assert end_request(token).as_dict()["cost_usd"] == 0.000021


# --------------------------------------------------------------------------- #
# The middleware that emits the event the dashboard reads
# --------------------------------------------------------------------------- #


def _request_completed_events(client_call) -> list[dict]:
    import structlog

    with structlog.testing.capture_logs() as captured:
        client_call()
    return [e for e in captured if e.get("event") == "request_completed"]


def test_every_real_request_emits_one_metric_event():
    from fastapi.testclient import TestClient

    from app.main import app

    # No ``with``: entering the lifespan would open the graph's Postgres pool,
    # which this test has no use for. Same pattern as tests/api/test_service_token.py.
    client = TestClient(app)
    events = _request_completed_events(lambda: client.get("/openapi.json"))

    assert len(events) == 1
    event = events[0]
    # These six keys ARE the dashboard's contract. Renaming one silently empties
    # a panel, so they are pinned here rather than discovered later.
    for key in ("path", "method", "status", "latency_ms", "cost_usd", "llm_calls"):
        assert key in event, key
    assert event["status"] == 200


def test_the_health_probe_is_not_counted():
    """It runs every 30 seconds. Counted, it would BE the metrics: the p95 of the
    service would really be the p95 of a liveness probe, and the error rate would
    be diluted into meaninglessness by traffic nobody asked for."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    assert _request_completed_events(lambda: client.get("/health")) == []
