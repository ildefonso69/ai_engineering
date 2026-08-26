#!/usr/bin/env python3
"""Session 16 — the regression gate: did this change make the system worse?

``ci.yml`` reserved this filename in its ``TODO(S16)`` block since Session 15.
This is it.

A single evaluation run tells you how good the system is. It cannot tell you
whether it just got worse, and "worse" is the question that decides whether
something ships. That needs a stored point of comparison — ``eval/baseline.json``,
versioned in git, promoted deliberately from a run somebody looked at.

    uv run python eval/compare_against_baseline.py                    # latest report
    uv run python eval/compare_against_baseline.py --report r.json
    uv run python eval/compare_against_baseline.py --tolerance 0.10

WHY A TOLERANCE. These metrics come from a real model over six cases: they move a
little between identical runs, and a gate that fires on noise gets switched off
within a week. The tolerance is what makes the gate survivable. It is a per-metric
number, not a global one, because the metrics do not deserve equal patience.

WHICH METRICS GATE, AND WHICH ONLY REPORT. Latency and cost are reported, never
gated: they belong to the dashboard, they move with the provider's load, and
failing a quality gate because OpenAI was busy teaches people to ignore it.

**``safety_compliance_rate`` has zero tolerance.** Accuracy is allowed to wobble;
inventing a number where the system used to abstain is not a wobble.

Exit codes: 0 clean · 1 regression · 2 nothing to compare against.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_BASELINE = EVAL_DIR / "baseline.json"
DEFAULT_REPORTS_DIR = EVAL_DIR / "reports"

DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"

# metric -> (higher_is_better, tolerance). Tolerance is in the metric's own units:
# a rate drops by at most this much, an error grows by at most this much.
GATED_METRICS: dict[str, tuple[bool, float]] = {
    "within_range_rate": (True, 0.15),
    "citation_validity_rate": (True, 0.05),
    "hallucination_rate": (False, 0.05),
    # Zero tolerance, and the only one. Everything else is quality; this is safety.
    "safety_compliance_rate": (True, 0.0),
}
REPORTED_ONLY = ("mean_absolute_error", "mean_latency_ms", "p95_latency_ms", "abstention_rate",
                 "escalation_rate")


def latest_report(reports_dir: Path) -> Path | None:
    reports = sorted(reports_dir.glob("report-*.json"))
    return reports[-1] if reports else None


def compare(current: dict[str, Any], baseline: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per gated metric, with a verdict. Pure: no printing, no exiting."""
    rows: list[dict[str, Any]] = []
    for metric, (higher_is_better, tolerance) in GATED_METRICS.items():
        now, before = current.get(metric), baseline.get(metric)
        if now is None or before is None:
            rows.append({"metric": metric, "now": now, "before": before,
                         "delta": None, "regressed": False, "note": "not measured"})
            continue
        delta = now - before
        # A move in the good direction is never a regression, however large.
        drop = -delta if higher_is_better else delta
        rows.append({
            "metric": metric,
            "now": now,
            "before": before,
            "delta": delta,
            "tolerance": tolerance,
            "regressed": drop > tolerance,
            "note": "",
        })
    return rows


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    parser.add_argument("--report", default=None, help="Defaults to the newest in eval/reports/")
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR))
    args = parser.parse_args(argv)

    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        print(
            f"{YELLOW}No baseline at {baseline_path}.{RESET}\n"
            f"{DIM}Promote a run you have actually looked at:\n"
            f"  uv run python eval/promote_baseline.py --report <report.json>{RESET}",
            file=sys.stderr,
        )
        return 2

    report_path = Path(args.report) if args.report else latest_report(Path(args.reports_dir))
    if report_path is None or not report_path.exists():
        print(f"{YELLOW}No evaluation report to compare.{RESET}", file=sys.stderr)
        return 2

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    rows = compare(report["metrics"], baseline["metrics"])

    print(f"Regression gate — {report_path.name} vs baseline {baseline.get('promoted_at', '?')}\n")
    print(f"{DIM}  {'metric':24s} {'baseline':>9s} {'now':>9s} {'delta':>9s}{RESET}")
    for row in rows:
        mark = f"{RED}REGRESSED{RESET}" if row["regressed"] else f"{GREEN}ok{RESET}"
        delta = "" if row["delta"] is None else f"{row['delta']:+.3f}"
        print(
            f"  {row['metric']:24s} {_fmt(row['before']):>9s} {_fmt(row['now']):>9s} "
            f"{delta:>9s}  {mark}"
        )

    print(f"\n{DIM}reported, not gated:{RESET}")
    for metric in REPORTED_ONLY:
        print(f"  {metric:24s} {_fmt(baseline['metrics'].get(metric)):>9s} "
              f"{_fmt(report['metrics'].get(metric)):>9s}")

    regressions = [r for r in rows if r["regressed"]]
    if regressions:
        print(f"\n{RED}QUALITY REGRESSION — {len(regressions)} metric(s) fell beyond tolerance"
              f"{RESET}")
        return 1
    print(f"\n{GREEN}No regression.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
