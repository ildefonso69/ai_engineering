#!/usr/bin/env python3
"""Session 16 — evaluation harness: run the golden set against the DEPLOYED service.

This is the opposite of the test suite. CI doubles the model because CI tests
*our code*; this harness calls the real model because it evaluates *the model*.
It therefore spends money, is non-deterministic, and must never gate an ordinary
commit — it runs deliberately, against a running system.

    uv run python eval/run_eval.py --base-url http://localhost:8000
    uv run python eval/run_eval.py --limit 1              # cheap smoke of the harness
    uv run python eval/run_eval.py --case case-006-avionics-certification-no-precedent

WHERE IT HAS TO RUN. Since Session 15 the AI service publishes no ports: from
outside, only the business backend is reachable, over HTTPS. So this harness runs
INSIDE the perimeter — on the instance, against ``http://localhost:8000`` from
within the ``ai-service`` container. That is not an obstacle to work around; it is
the deployment boundary doing its job.

AUTHENTICATION is the two independent Session 15 layers, both read from the
environment and never hardcoded:

* ``X-Service-Token`` (``AI_SERVICE_TOKEN``) — may you talk to this service at all
* ``X-API-Key`` (``ESTIMATE_API_KEY``)       — may you call this particular router

Exit code is 0 only if every case passes, so the same file can later be wired as
a regression gate without being rewritten.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_GOLDEN_SET = EVAL_DIR / "golden_set.json"
DEFAULT_REPORTS_DIR = EVAL_DIR / "reports"

ENDPOINT = "/v1/estimate/from-transcript"

# The endpoint is rate limited to 10/minute. Stay under it with a margin rather
# than discovering the limit as a 429 halfway through a paid run.
MAX_CALLS_PER_MINUTE = 8

DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"


# --------------------------------------------------------------------------- #
# Golden set
# --------------------------------------------------------------------------- #


def load_golden_set(path: Path | str = DEFAULT_GOLDEN_SET) -> list[dict[str, Any]]:
    """Load and sanity-check the golden set.

    The checks are deliberately strict: a golden set that silently loses its
    abstention case still produces a healthy-looking report, which is worse than
    no report at all.
    """
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{path}: expected a non-empty list of cases")

    seen: set[str] = set()
    for case in cases:
        case_id = case.get("id")
        if not case_id or case_id in seen:
            raise ValueError(f"{path}: missing or duplicated case id {case_id!r}")
        seen.add(case_id)
        # EstimateRequest enforces this server-side; failing here costs nothing,
        # failing there costs a round trip and a confusing 422.
        if len(case.get("transcript", "")) < 100:
            raise ValueError(f"{case_id}: transcript must be at least 100 characters")
        if not case.get("expect_abstention"):
            low, high = case.get("acceptable_range") or (None, None)
            if low is None or high is None or low > high:
                raise ValueError(f"{case_id}: an estimation case needs a valid acceptable_range")

    if not any(c.get("expect_abstention") for c in cases):
        raise ValueError(f"{path}: the golden set must contain at least one abstention case")
    return cases


# --------------------------------------------------------------------------- #
# Calling the service
# --------------------------------------------------------------------------- #


def build_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("AI_SERVICE_TOKEN")
    api_key = os.environ.get("ESTIMATE_API_KEY")
    if token:
        headers["X-Service-Token"] = token
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def call_estimate(
    client: httpx.Client, case: dict[str, Any], *, timeout: float
) -> dict[str, Any]:
    """POST one transcript and time it.

    NOTE the absence of ``idempotency_key``. Sending one would return the stored
    estimate from a previous run: the request would look fast and free, and the
    evaluation would measure nothing at all. Every case here is meant to be a
    fresh call.
    """
    started = time.perf_counter()
    try:
        response = client.post(
            ENDPOINT,
            json={"transcript": case["transcript"]},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return {
            "status": 0,
            "body": {},
            "latency_ms": (time.perf_counter() - started) * 1000,
            "transport_error": f"{type(exc).__name__}: {exc}",
        }

    latency_ms = (time.perf_counter() - started) * 1000
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:500]}
    return {"status": response.status_code, "body": body, "latency_ms": latency_ms}


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def _dangling_line_citations(body: dict[str, Any]) -> tuple[int, int]:
    """(lines citing a source the estimate never declares, total grounded lines).

    A hallucination the harness can see from OUTSIDE the service: every line
    citation must point at a ``chunk_id`` that appears in the estimate's own
    top-level ``sources``. A line that cites an id nobody declared is a number
    wearing a citation — the most convincing kind of wrong answer, because it
    looks sourced.

    The service already checks this internally (``verify_citations``). Checking it
    again from the client side is not redundant: it is how you find out when the
    internal check stops running.
    """
    declared = {str(s.get("chunk_id")) for s in (body.get("sources") or [])}
    grounded = [
        task
        for module in (body.get("modules") or [])
        for task in (module.get("tasks") or [])
        if task.get("grounded")
    ]
    dangling = sum(
        1
        for task in grounded
        if any(str(s.get("chunk_id")) not in declared for s in (task.get("sources") or []))
    )
    return dangling, len(grounded)


def _citation_validity(body: dict[str, Any]) -> bool | None:
    """Every grounded task line must cite at least one source.

    ``TaskItem`` already enforces this server-side, so a False here means the
    contract broke — which is exactly the kind of thing worth noticing from the
    outside rather than trusting.
    """
    modules = body.get("modules") or []
    lines = [task for module in modules for task in (module.get("tasks") or [])]
    if not lines:
        return None
    return all(
        (task.get("sources") or []) if task.get("grounded") else not (task.get("sources") or [])
        for task in lines
    )


def evaluate_case(case: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Compare one response against what we decided a good answer looks like."""
    body = result["body"] if isinstance(result.get("body"), dict) else {}
    dangling, grounded_lines = _dangling_line_citations(body)
    base: dict[str, Any] = {
        "id": case["id"],
        "difficulty": case.get("difficulty", "unknown"),
        "status": result["status"],
        "latency_ms": round(result["latency_ms"], 1),
        "confidence": body.get("confidence"),
        "abstained": body.get("confidence") == "insufficient",
        # Session 16: written by the output guardrail, never by the model.
        "requires_human_review": bool(body.get("requires_human_review")),
        "review_reasons": body.get("review_reasons") or [],
        "dangling_line_citations": dangling,
        "grounded_lines": grounded_lines,
    }

    if result["status"] != 200:
        detail = result.get("transport_error") or str(body)[:200]
        return {**base, "type": "error", "passed": False, "detail": detail}

    predicted = body.get("total_engineer_days")
    confidence = body.get("confidence")

    # --- abstention: the safety case ---------------------------------------
    if case.get("expect_abstention"):
        abstained = confidence == "insufficient" and predicted is None
        return {
            **base,
            "type": "abstention",
            "passed": abstained,
            "predicted": predicted,
            "detail": (
                "abstained correctly"
                if abstained
                else f"answered {predicted} engineer-days with confidence={confidence!r}"
            ),
        }

    # --- estimation ---------------------------------------------------------
    low, high = case["acceptable_range"]
    within_range = predicted is not None and low <= predicted <= high
    expected = case.get("expected_engineer_days")
    abs_error = abs(predicted - expected) if (predicted is not None and expected) else None

    return {
        **base,
        "type": "estimation",
        "passed": within_range,
        "predicted": predicted,
        "expected": expected,
        "acceptable_range": [low, high],
        "abs_error": abs_error,
        "sources": len(body.get("sources") or []),
        "citation_validity": _citation_validity(body),
        "detail": (
            "within range"
            if within_range
            else f"{predicted} outside [{low}, {high}]"
            if predicted is not None
            else f"no number (confidence={confidence!r})"
        ),
    }


def summarise(evaluations: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the per-case verdicts into the report's headline metrics."""
    estimations = [e for e in evaluations if e["type"] == "estimation"]
    abstentions = [e for e in evaluations if e["type"] == "abstention"]
    errors = [e["abs_error"] for e in estimations if e.get("abs_error") is not None]
    latencies = [e["latency_ms"] for e in evaluations]
    citation_flags = [
        e["citation_validity"] for e in estimations if e.get("citation_validity") is not None
    ]
    total_grounded_lines = sum(e.get("grounded_lines", 0) for e in evaluations)

    return {
        "within_range_rate": (
            len([e for e in estimations if e["passed"]]) / len(estimations) if estimations else None
        ),
        "mean_absolute_error": (sum(errors) / len(errors)) if errors else None,
        # ``all([])`` is True, which would report a golden set with no abstention
        # case as safe. load_golden_set refuses that set, and this stays explicit.
        "abstention_correct": (
            all(e["passed"] for e in abstentions) if abstentions else None
        ),
        "mean_latency_ms": (sum(latencies) / len(latencies)) if latencies else None,
        "p95_latency_ms": _percentile(latencies, 95),
        "citation_validity_rate": (
            len([f for f in citation_flags if f]) / len(citation_flags) if citation_flags else None
        ),
        # --- Session 16 KPI board -------------------------------------------
        # Accuracy alone is a bad summary of an estimator: a system that answers
        # everything confidently scores well on within_range_rate right up until
        # the day it invents a number for a project nobody has ever done.
        #
        # hallucination_rate  — lines citing a source the estimate never declared
        # safety_compliance   — abstained or escalated exactly when it should have
        # abstention_rate     — how often it declines AT ALL (a rate near 1 means
        #                       a system that is safe by being useless)
        # escalation_rate     — how much work it sends to a person, which is a
        #                       real operating cost, not a free safety net
        "hallucination_rate": (
            sum(e.get("dangling_line_citations", 0) for e in evaluations)
            / total_grounded_lines
            if total_grounded_lines
            else None
        ),
        "safety_compliance_rate": (
            len([e for e in evaluations if _is_safety_compliant(e)]) / len(evaluations)
            if evaluations
            else None
        ),
        "abstention_rate": (
            len([e for e in evaluations if e.get("abstained")]) / len(evaluations)
            if evaluations
            else None
        ),
        "escalation_rate": (
            len([e for e in evaluations if e.get("requires_human_review")]) / len(evaluations)
            if evaluations
            else None
        ),
        "cases_total": len(evaluations),
        "cases_passed": len([e for e in evaluations if e["passed"]]),
    }


def _is_safety_compliant(evaluation: dict[str, Any]) -> bool:
    """Did the system do the safe thing for THIS case?

    An abstention case is compliant when it abstained. An estimation case is
    compliant when it either produced a number nobody needs to double-check, or
    produced a doubtful one and said so. The failure this catches is the quiet
    one: a wrong number delivered with no flag at all.
    """
    if evaluation["type"] == "abstention":
        return bool(evaluation["passed"])
    if evaluation["type"] == "error":
        return False
    return bool(evaluation["passed"] or evaluation.get("requires_human_review"))


def _percentile(values: list[float], pct: int) -> float | None:
    """Nearest-rank percentile. Explicit because ``statistics.quantiles`` needs
    at least two points and a six-case run can legitimately have one."""
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(round(pct / 100 * len(ordered) + 0.5))))
    return round(ordered[rank - 1], 1)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #


def run(
    cases: list[dict[str, Any]], *, base_url: str, timeout: float, verbose: bool = True
) -> dict[str, Any]:
    evaluations: list[dict[str, Any]] = []
    call_times: list[float] = []
    started_at = datetime.now(timezone.utc)

    with httpx.Client(base_url=base_url.rstrip("/"), headers=build_headers()) as client:
        for index, case in enumerate(cases, start=1):
            _throttle(call_times)
            if verbose:
                print(f"{DIM}[{index}/{len(cases)}] {case['id']}…{RESET}", flush=True)
            call_times.append(time.monotonic())
            result = call_estimate(client, case, timeout=timeout)
            evaluation = evaluate_case(case, result)
            evaluations.append(evaluation)
            if verbose:
                print("  " + _format_row(evaluation), flush=True)

    return {
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "endpoint": ENDPOINT,
        "metrics": summarise(evaluations),
        "cases": evaluations,
    }


def _throttle(call_times: list[float]) -> None:
    """Keep under the endpoint's 10/minute limit."""
    recent = [t for t in call_times if time.monotonic() - t < 60]
    if len(recent) >= MAX_CALLS_PER_MINUTE:
        wait = 60 - (time.monotonic() - recent[0]) + 1
        if wait > 0:
            print(f"{DIM}  rate limit: waiting {wait:.0f}s{RESET}", flush=True)
            time.sleep(wait)


def _format_row(ev: dict[str, Any]) -> str:
    mark = f"{GREEN}PASS{RESET}" if ev["passed"] else f"{RED}FAIL{RESET}"
    return f"{mark}  {ev['type']:11s} {ev['detail']}  {DIM}{ev['latency_ms'] / 1000:.1f}s{RESET}"


def _print_report(report: dict[str, Any]) -> None:
    m = report["metrics"]
    print(f"\n{DIM}Metrics{RESET}")

    def fmt(value: Any, suffix: str = "", pct: bool = False) -> str:
        if value is None:
            return f"{YELLOW}n/a{RESET}"
        if isinstance(value, bool):
            return f"{GREEN}yes{RESET}" if value else f"{RED}no{RESET}"
        return f"{value * 100:.0f}%" if pct else f"{value:.1f}{suffix}"

    print(f"  within_range_rate       {fmt(m['within_range_rate'], pct=True)}")
    print(f"  mean_absolute_error     {fmt(m['mean_absolute_error'], ' engineer-days')}")
    print(f"  abstention_correct      {fmt(m['abstention_correct'])}")
    print(f"  mean_latency_ms         {fmt(m['mean_latency_ms'], ' ms')}")
    print(f"  p95_latency_ms          {fmt(m['p95_latency_ms'], ' ms')}")
    print(f"  citation_validity_rate  {fmt(m['citation_validity_rate'], pct=True)}")
    print(f"  hallucination_rate      {fmt(m['hallucination_rate'], pct=True)}")
    print(f"  safety_compliance_rate  {fmt(m['safety_compliance_rate'], pct=True)}")
    print(f"  abstention_rate         {fmt(m['abstention_rate'], pct=True)}")
    print(f"  escalation_rate         {fmt(m['escalation_rate'], pct=True)}")
    print(f"\n{m['cases_passed']}/{m['cases_total']} cases passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default=os.environ.get("EVAL_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--golden-set", default=str(DEFAULT_GOLDEN_SET))
    parser.add_argument("--out", default=str(DEFAULT_REPORTS_DIR), help="Directory for the report")
    # gpt-5 with high reasoning effort takes 1-3 minutes per case; the default
    # httpx timeout of 5s would fail every single one of them.
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--limit", type=int, default=None, help="Run only the first N cases")
    parser.add_argument("--case", default=None, help="Run a single case by id")
    args = parser.parse_args(argv)

    cases = load_golden_set(args.golden_set)
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"{RED}no case with id {args.case!r}{RESET}", file=sys.stderr)
            return 2
    if args.limit:
        cases = cases[: args.limit]

    print(f"Evaluation — {len(cases)} case(s) against {args.base_url}{ENDPOINT}")
    if "X-Service-Token" not in build_headers():
        print(f"{YELLOW}  AI_SERVICE_TOKEN unset — fine locally, a 401 against the instance{RESET}")

    report = run(cases, base_url=args.base_url, timeout=args.timeout)
    _print_report(report)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["started_at"].replace(":", "").replace("-", "").split(".")[0]
    out_path = out_dir / f"report-{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"{DIM}report → {out_path}{RESET}")

    return 0 if report["metrics"]["cases_passed"] == report["metrics"]["cases_total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
