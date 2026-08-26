#!/usr/bin/env python3
"""Session 16 — A against B, quality and cost on the same table.

    uv run python eval/run_eval.py --variant a
    uv run python eval/run_eval.py --variant b
    uv run python eval/compare_variants.py

THE POINT OF THE TABLE. An A/B test on an LLM system has two columns, and reading
either one alone gives the wrong answer:

* B is 60% cheaper and estimates worse → not an improvement, it is a discount on
  being wrong.
* B is 5% cheaper and identical → not worth two code paths that will drift.
* B is cheaper AND holds quality → ship it, and the number that says so is
  ``within_range_rate``, not the invoice.

Cost per run is not in the harness report — the harness measures the CLIENT side
and cannot see tokens. It comes from the dashboard over the same window:

    docker compose logs --no-log-prefix ai-service | python eval/dashboard.py --json d.json
    python eval/compare_variants.py --dashboard d.json

Without ``--dashboard`` the quality half still prints, and says so rather than
quietly leaving the cost column blank as though it were zero.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_REPORTS_DIR = EVAL_DIR / "reports"

DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"

# metric -> higher_is_better
QUALITY_METRICS = {
    "within_range_rate": True,
    "mean_absolute_error": False,
    "safety_compliance_rate": True,
    "hallucination_rate": False,
    "abstention_rate": None,      # neither: context, not a score
    "p95_latency_ms": False,
}


def latest_for_variant(reports_dir: Path, variant: str) -> Path | None:
    """Newest report whose run forced this arm.

    Matched on the report's own ``variant`` field rather than the filename: a
    report that says nothing about its arm is a report that cannot be compared,
    and silently treating it as A is how you publish a conclusion about a run
    that never happened.
    """
    candidates = []
    for path in sorted(reports_dir.glob("report-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("variant") == variant:
            candidates.append(path)
    return candidates[-1] if candidates else None


def _cost_by_variant(dashboard: dict[str, Any] | None) -> dict[str, float]:
    if not dashboard:
        return {}
    return {
        variant: row.get("cost_mean_usd", 0.0)
        for variant, row in (dashboard.get("by_variant") or {}).items()
    }


def _fmt(value: Any, higher_is_better: bool | None) -> str:
    if value is None:
        return f"{YELLOW}n/a{RESET}"
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR))
    parser.add_argument("--report-a", default=None)
    parser.add_argument("--report-b", default=None)
    parser.add_argument("--dashboard", default=None, help="dashboard.py --json output, for cost")
    args = parser.parse_args(argv)

    reports_dir = Path(args.reports_dir)
    path_a = Path(args.report_a) if args.report_a else latest_for_variant(reports_dir, "a")
    path_b = Path(args.report_b) if args.report_b else latest_for_variant(reports_dir, "b")
    if path_a is None or path_b is None:
        print(
            f"{YELLOW}Need one report per arm.{RESET}\n"
            f"{DIM}  uv run python eval/run_eval.py --variant a\n"
            f"  uv run python eval/run_eval.py --variant b{RESET}"
        )
        return 2

    a = json.loads(path_a.read_text(encoding="utf-8"))
    b = json.loads(path_b.read_text(encoding="utf-8"))
    dashboard = json.loads(Path(args.dashboard).read_text()) if args.dashboard else None
    costs = _cost_by_variant(dashboard)

    print(f"A/B — {path_a.name} vs {path_b.name}\n")
    print(f"{DIM}  {'metric':24s} {'A':>10s} {'B':>10s} {'delta':>10s}{RESET}")
    for metric, higher_is_better in QUALITY_METRICS.items():
        va, vb = a["metrics"].get(metric), b["metrics"].get(metric)
        delta = "" if (va is None or vb is None) else f"{vb - va:+.3f}"
        verdict = ""
        if va is not None and vb is not None and higher_is_better is not None:
            better = (vb > va) if higher_is_better else (vb < va)
            worse = (vb < va) if higher_is_better else (vb > va)
            verdict = f"{GREEN}B better{RESET}" if better else (
                f"{RED}B worse{RESET}" if worse else f"{DIM}same{RESET}")
        print(
            f"  {metric:24s} {_fmt(va, higher_is_better):>10s} "
            f"{_fmt(vb, higher_is_better):>10s} {delta:>10s}  {verdict}"
        )

    print(f"\n{DIM}  cost per request{RESET}")
    if costs:
        cost_a, cost_b = costs.get("a"), costs.get("b")
        print(f"  {'cost_mean_usd':24s} {cost_a if cost_a is None else f'{cost_a:.4f}':>10} "
              f"{cost_b if cost_b is None else f'{cost_b:.4f}':>10}")
        if cost_a and cost_b:
            print(f"  {DIM}B is {(1 - cost_b / cost_a) * 100:.0f}% cheaper per request{RESET}")
    else:
        print(f"  {YELLOW}no dashboard data — pass --dashboard to fill the cost column.{RESET}")
        print(f"  {DIM}Quality alone cannot decide an A/B: a cheaper variant that")
        print(f"  estimates worse is a discount on being wrong.{RESET}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
