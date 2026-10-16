"""Evaluation harness for the RAG estimation pipeline's golden set.

Runs the golden set (``eval/golden_set.json``, built and live-verified in Paso
1) against the REAL, deployed ``estimator`` service -- no mocking, per the
exercise's own requirement. Each case POSTs to the real
``POST /v1/estimate/from-transcript`` endpoint (``app/api/routers/estimate.py``)
and is scored against:

- Abstention cases (``expect_abstention: true``): pass iff the real response's
  ``confidence == "insufficient"``. This schema has no float confidence score
  (see ``app/generation/rag/schemas.py::Estimate``), so there is no threshold
  to tune here -- it's a literal string match on the actual business signal.
- Estimation cases: pass iff ``total_engineer_days`` is not None AND falls
  inside ``case["acceptable_range"]`` (inclusive). The golden set deliberately
  carries a *range*, not a single "expected value" -- an estimate is a
  reasonable interval, not an exact figure -- so there is no ``abs_error``
  against a value that doesn't exist. We instead report
  ``distance_from_range_midpoint`` as a secondary, clearly-labelled metric.
- Citation accuracy (all cases): what fraction of
  ``case["expected_sources_include"]`` actually appear in the response's
  ``sources[].source_id``, reported as ``citation_recall``.

Usage (from ``estimator/``):

    uv run python eval/run_eval.py
    uv run python eval/run_eval.py --golden-set eval/golden_set.json --base-url http://localhost:8000

Configuration is environment-first (never hardcoded secrets):

    AI_SERVICE_URL      base URL of the estimator service (default http://localhost:8000)
    ESTIMATE_API_KEY    API key for X-API-Key header, required by
                         require_estimate_key (app/api/security.py). Fails
                         loudly if unset and no --api-key override is given.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://localhost:8000"
ENDPOINT_PATH = "/v1/estimate/from-transcript"

# The real pipeline (reformulate -> embed -> retrieve -> assemble -> generate
# with gpt-5 at reasoning_effort="high", up to GENERATION_MAX_TOKENS=64000
# reasoning+output tokens) has been observed end-to-end anywhere from ~30s to
# several minutes per case. 30s (the reference sketch's default) would
# spuriously fail nearly every real call. 180s gives real headroom without
# hanging forever on a genuinely stuck request.
DEFAULT_TIMEOUT_SECONDS = 180.0


def load_golden_set(path: Path) -> list[dict[str, Any]]:
    """Load the golden set JSON array from disk."""
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array of cases in {path}, got {type(data)}")
    return data


def call_estimate(
    case: dict[str, Any],
    base_url: str,
    api_key: str,
    client: httpx.Client,
) -> dict[str, Any]:
    """POST one golden-set case to the real /v1/estimate/from-transcript endpoint.

    Deliberately omits ``idempotency_key``. The golden set exists to catch
    regressions after a prompt/model/retrieval change, and the idempotency
    store's TTL (24h, see ``app/generation/rag/idempotency.py``) is longer
    than a typical iteration cycle — reusing a stable key (the case's own
    ``id``) across runs would silently return a stale pre-change cached
    result and make the harness report "nothing changed" when it did. Each
    run genuinely re-executes the pipeline; this costs real tokens, on
    purpose (see docs/evaluation.md).
    """
    url = f"{base_url.rstrip('/')}{ENDPOINT_PATH}"
    payload = {
        "transcript": case["transcript"],
    }
    headers = {"X-API-Key": api_key}

    start = time.monotonic()
    try:
        response = client.post(url, json=payload, headers=headers)
        latency_s = time.monotonic() - start
        try:
            body = response.json()
        except ValueError:
            body = {"_raw_text": response.text}
        return {
            "status": response.status_code,
            "body": body,
            "latency_s": latency_s,
            "transport_error": None,
        }
    except httpx.TimeoutException as exc:
        latency_s = time.monotonic() - start
        return {
            "status": None,
            "body": None,
            "latency_s": latency_s,
            "transport_error": f"timeout: {exc}",
        }
    except httpx.HTTPError as exc:
        latency_s = time.monotonic() - start
        return {
            "status": None,
            "body": None,
            "latency_s": latency_s,
            "transport_error": f"http_error: {exc}",
        }


def _citation_recall(expected_sources: list[int], returned_sources: list[dict[str, Any]]) -> float | None:
    """Fraction of expected_sources_include present in response sources[].source_id.

    Returns None when the case has no expected sources to check (e.g. the
    abstention case, which expects an empty source list) rather than a
    misleading 1.0 or 0.0 for an empty "expectation".
    """
    if not expected_sources:
        return None
    returned_ids = {s.get("source_id") for s in returned_sources if isinstance(s, dict)}
    hits = sum(1 for sid in expected_sources if sid in returned_ids)
    return hits / len(expected_sources)


def evaluate_case(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Score one case's real response against the golden set's expectations."""
    evaluation: dict[str, Any] = {
        "id": case["id"],
        "category": case.get("category"),
        "http_status": result["status"],
        "latency_s": round(result["latency_s"], 3),
        "transport_ok": result["transport_error"] is None and result["status"] == 200,
        "transport_error": result["transport_error"],
    }

    if not evaluation["transport_ok"]:
        # A transport/pipeline failure is a different failure mode than "wrong
        # estimate" -- surface it distinctly instead of silently failing the
        # range/confidence check below with no explanation. Still tag "type"
        # so aggregate()'s within_range_rate / abstention_correct split can
        # count this case correctly even though it never reached scoring.
        evaluation["type"] = "abstention" if case.get("expect_abstention") else "estimation"
        evaluation["passed"] = False
        evaluation["failure_reason"] = "transport_or_http_error"
        evaluation["citation_recall"] = None
        return evaluation

    body = result["body"] or {}
    confidence = body.get("confidence")
    total_days = body.get("total_engineer_days")
    sources = body.get("sources") or []

    expected_sources = case.get("expected_sources_include") or []
    evaluation["citation_recall"] = _citation_recall(expected_sources, sources)
    evaluation["returned_confidence"] = confidence
    evaluation["returned_total_engineer_days"] = total_days
    evaluation["returned_source_ids"] = [
        s.get("source_id") for s in sources if isinstance(s, dict)
    ]

    if case.get("expect_abstention"):
        evaluation["type"] = "abstention"
        evaluation["passed"] = confidence == "insufficient"
        evaluation["failure_reason"] = None if evaluation["passed"] else "expected_abstention_not_returned"
        return evaluation

    evaluation["type"] = "estimation"
    acceptable_range = case.get("acceptable_range")
    if not acceptable_range or len(acceptable_range) != 2:
        evaluation["passed"] = False
        evaluation["failure_reason"] = "golden_set_missing_acceptable_range"
        return evaluation

    low, high = acceptable_range
    midpoint = (low + high) / 2
    if total_days is None:
        evaluation["passed"] = False
        evaluation["failure_reason"] = "total_engineer_days_is_none"
        evaluation["distance_from_range_midpoint"] = None
        return evaluation

    in_range = low <= total_days <= high
    evaluation["passed"] = in_range
    evaluation["failure_reason"] = None if in_range else "total_engineer_days_outside_acceptable_range"
    evaluation["distance_from_range_midpoint"] = total_days - midpoint
    return evaluation


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[idx]


def aggregate(evaluations: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up per-case evaluations into run-level metrics.

    Includes the four minimum metrics the exercise requires
    (``within_range_rate``, ``mean_absolute_error``, ``abstention_correct``,
    ``mean_latency_ms``) plus the richer breakdown already built in Paso 2
    (per-category pass rate, citation recall, latency percentiles, transport
    failures) -- the spec asks for a minimum, not a ceiling.
    """
    total = len(evaluations)
    passed = [e for e in evaluations if e["passed"]]
    http_failures = [e for e in evaluations if not e["transport_ok"]]
    latencies = [e["latency_s"] for e in evaluations]

    estimation_cases = [e for e in evaluations if e["type"] == "estimation"]
    estimation_passed = [e for e in estimation_cases if e["passed"]]
    abstention_cases = [e for e in evaluations if e["type"] == "abstention"]

    # mean_absolute_error: this golden set has no single "expected" value by
    # design (an estimate is a range, not a point figure -- see module
    # docstring), so there is no literal absolute-error-against-a-point to
    # average. We use |distance_from_range_midpoint| as the closest honest
    # analog: how far the real estimate landed from the center of the
    # acceptable range, in engineer-days. Cases where that distance is
    # undefined (transport failure, total_engineer_days is None) are
    # excluded from the mean rather than coerced to 0.
    abs_errors = [
        abs(e["distance_from_range_midpoint"])
        for e in estimation_cases
        if e.get("distance_from_range_midpoint") is not None
    ]

    by_category: dict[str, dict[str, Any]] = {}
    for e in evaluations:
        cat = e.get("category") or "uncategorized"
        bucket = by_category.setdefault(cat, {"total": 0, "passed": 0})
        bucket["total"] += 1
        if e["passed"]:
            bucket["passed"] += 1
    for cat, bucket in by_category.items():
        bucket["pass_rate"] = bucket["passed"] / bucket["total"] if bucket["total"] else None

    citation_recalls = [
        e["citation_recall"] for e in evaluations if e.get("citation_recall") is not None
    ]

    return {
        # --- Minimum metrics required by Paso 3 ---
        "within_range_rate": (
            len(estimation_passed) / len(estimation_cases) if estimation_cases else None
        ),
        "mean_absolute_error": statistics.fmean(abs_errors) if abs_errors else None,
        "abstention_correct": all(e["passed"] for e in abstention_cases) if abstention_cases else None,
        "mean_latency_ms": statistics.fmean(e * 1000 for e in latencies) if latencies else None,
        # --- Richer breakdown (Paso 2, kept as extras) ---
        "total_cases": total,
        "passed_cases": len(passed),
        "overall_pass_rate": len(passed) / total if total else None,
        "pass_rate_by_category": by_category,
        "latency_s": {
            "avg": statistics.fmean(latencies) if latencies else None,
            "median": statistics.median(latencies) if latencies else None,
            "p95": _percentile(latencies, 95),
        },
        "avg_citation_recall": (
            statistics.fmean(citation_recalls) if citation_recalls else None
        ),
        "http_failure_count": len(http_failures),
        "http_failure_ids": [e["id"] for e in http_failures],
    }


def _resolve_api_key(cli_api_key: str | None) -> str:
    """Resolve the X-API-Key value: CLI override, then ESTIMATE_API_KEY env var.

    Fails loudly (not silently sending no auth) if neither is set, since
    require_estimate_key (app/api/security.py) rejects every request with a
    401 when the key doesn't match -- better to fail fast with a clear
    message than burn real LLM calls on a run that can never succeed.
    """
    if cli_api_key:
        return cli_api_key
    env_key = os.environ.get("ESTIMATE_API_KEY")
    if env_key:
        return env_key
    raise SystemExit(
        "ESTIMATE_API_KEY is not set (and no --api-key override was given). "
        "This harness calls the real, authenticated /v1/estimate/from-transcript "
        "endpoint (require_estimate_key in app/api/security.py) -- set "
        "ESTIMATE_API_KEY in the environment (matching the estimator service's "
        "own .env) or pass --api-key explicitly."
    )


def run(golden_set_path: Path, base_url: str, api_key: str) -> dict[str, Any]:
    cases = load_golden_set(golden_set_path)
    evaluations: list[dict[str, Any]] = []

    with httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        for case in cases:
            print(f"Running case {case['id']} (category={case.get('category')})...", flush=True)
            result = call_estimate(case, base_url, api_key, client)
            evaluation = evaluate_case(case, result)
            evaluations.append(evaluation)
            status_label = "PASS" if evaluation["passed"] else "FAIL"
            print(
                f"  -> {status_label} "
                f"(http={evaluation['http_status']}, latency={evaluation['latency_s']}s, "
                f"citation_recall={evaluation.get('citation_recall')})",
                flush=True,
            )

    metrics = aggregate(evaluations)
    report = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "endpoint": ENDPOINT_PATH,
        "golden_set_path": str(golden_set_path),
        "case_evaluations": evaluations,
        "metrics": metrics,
    }
    return report


def print_summary(report: dict[str, Any]) -> None:
    metrics = report["metrics"]
    print("\n=== Evaluation summary ===")
    print(f"Base URL: {report['base_url']}{report['endpoint']}")
    print(f"Total cases: {metrics['total_cases']}")
    wrr = metrics["within_range_rate"]
    print(f"within_range_rate: {wrr:.0%}" if wrr is not None else "within_range_rate: n/a (no estimation cases)")
    mae = metrics["mean_absolute_error"]
    print(f"mean_absolute_error: {mae:.1f} engineer-days (vs. range midpoint)" if mae is not None else "mean_absolute_error: n/a")
    print(f"abstention_correct: {metrics['abstention_correct']}")
    mlat = metrics["mean_latency_ms"]
    print(f"mean_latency_ms: {mlat:.0f}" if mlat is not None else "mean_latency_ms: n/a")
    print(
        f"Overall pass rate: {metrics['passed_cases']}/{metrics['total_cases']} "
        f"({metrics['overall_pass_rate']:.0%})"
        if metrics["overall_pass_rate"] is not None
        else "Overall pass rate: n/a"
    )
    print("Pass rate by category:")
    for cat, bucket in metrics["pass_rate_by_category"].items():
        rate = bucket["pass_rate"]
        rate_str = f"{rate:.0%}" if rate is not None else "n/a"
        print(f"  - {cat}: {bucket['passed']}/{bucket['total']} ({rate_str})")
    lat = metrics["latency_s"]
    print(
        f"Latency (s): avg={lat['avg']:.1f} median={lat['median']:.1f} p95={lat['p95']:.1f}"
        if lat["avg"] is not None
        else "Latency (s): n/a"
    )
    avg_recall = metrics["avg_citation_recall"]
    print(f"Average citation recall: {avg_recall:.2f}" if avg_recall is not None else "Average citation recall: n/a")
    print(f"HTTP/transport failures: {metrics['http_failure_count']} {metrics['http_failure_ids']}")

    print("\nPer-case detail:")
    for e in report["case_evaluations"]:
        print(
            f"  [{('PASS' if e['passed'] else 'FAIL')}] {e['id']} "
            f"(category={e['category']}, http={e['http_status']}, "
            f"latency={e['latency_s']}s, citation_recall={e.get('citation_recall')}, "
            f"reason={e.get('failure_reason')})"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the golden-set evaluation harness against the real estimator service.")
    parser.add_argument(
        "--golden-set",
        default="eval/golden_set.json",
        help="Path to the golden set JSON array (default: eval/golden_set.json, relative to cwd).",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Base URL of the estimator service. Overrides AI_SERVICE_URL env var. "
        f"Falls back to {DEFAULT_BASE_URL} if neither is set.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="X-API-Key value. Overrides ESTIMATE_API_KEY env var.",
    )
    parser.add_argument(
        "--report-out",
        default="eval/last_run_report.json",
        help="Where to write the latest-run convenience copy (default: eval/last_run_report.json).",
    )
    parser.add_argument(
        "--reports-dir",
        default="eval/reports",
        help="Directory to append this run's timestamped report to, building the "
        "historical series the exercise asks for (default: eval/reports).",
    )
    args = parser.parse_args(argv)

    base_url = args.base_url or os.environ.get("AI_SERVICE_URL") or DEFAULT_BASE_URL
    api_key = _resolve_api_key(args.api_key)
    golden_set_path = Path(args.golden_set)

    report = run(golden_set_path, base_url, api_key)
    print_summary(report)

    # "Latest" convenience copy -- always overwritten, for quick inspection.
    report_path = Path(args.report_out)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\nFull machine-readable report written to {report_path.resolve()}")

    # Historical, timestamped series -- never overwritten. This is what lets
    # a later run be compared against past ones to detect quality
    # degradation (Paso 3's explicit purpose for persisting every run).
    run_timestamp = report["run_at"].replace(":", "").replace("+00:00", "Z")
    reports_dir = Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamped_path = reports_dir / f"report_{run_timestamp}.json"
    with timestamped_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"Historical report appended to {timestamped_path.resolve()}")

    return 0 if report["metrics"]["overall_pass_rate"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
