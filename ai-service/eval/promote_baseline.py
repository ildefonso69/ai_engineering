#!/usr/bin/env python3
"""Promote an evaluation report to THE baseline (Session 16).

    uv run python eval/promote_baseline.py --report eval/reports/report-....json

Deliberately a separate, manual step. Auto-promoting every run would make the
baseline follow the system wherever it drifts: quality could fall five points a
week, every single comparison would be green, and the gate would certify the
decline. A baseline is a decision — "this is the quality we agreed to hold" —
so a person makes it, and git records who and when.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", required=True)
    parser.add_argument("--baseline", default=str(EVAL_DIR / "baseline.json"))
    parser.add_argument("--note", default="", help="Why this run is the reference")
    args = parser.parse_args(argv)

    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    baseline = {
        "promoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "promoted_from": Path(args.report).name,
        "note": args.note,
        # Kept alongside the numbers because a baseline measured against a
        # different golden set is not a baseline, it is a coincidence.
        "cases_total": report["metrics"].get("cases_total"),
        "endpoint": report.get("endpoint"),
        "metrics": report["metrics"],
    }
    Path(args.baseline).write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    print(f"baseline ← {Path(args.report).name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
