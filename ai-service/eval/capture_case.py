#!/usr/bin/env python3
"""Turn a production failure into a golden-set case (Session 16).

The loop that closes the loop. Without it, evaluation only ever measures the
cases somebody thought of on the day they wrote the golden set, and the failures
that actually happen — the ones real users found — stay outside the measurement
forever. Every one of those is a regression waiting to happen twice.

    # a transcript that came back wrong in production
    uv run python eval/capture_case.py --transcript-file bad.txt \\
        --id case-007-erp-integration --expected 140 --range 90 210 \\
        --notes "Production run 2026-08-26: returned 640, hours read as days"

    # one that answered when it should have declined
    uv run python eval/capture_case.py --transcript-file bad.txt \\
        --id case-008-embedded-firmware --expect-abstention \\
        --notes "No precedent in the corpus; answered 310 anyway"

The new case is validated with the same loader the harness uses, so a malformed
capture fails here rather than three weeks later in the middle of a gate run.

DELIBERATELY NOT AUTOMATIC. It takes a human decision — what SHOULD this have
answered? — and that is exactly the judgement a golden set is made of. A script
that scraped failures and invented their expected values would be measuring the
system against itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_GOLDEN_SET = EVAL_DIR / "golden_set.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--transcript-file", default=None, help="Defaults to stdin")
    parser.add_argument("--id", required=True)
    parser.add_argument("--golden-set", default=str(DEFAULT_GOLDEN_SET))
    parser.add_argument("--expected", type=int, default=None, help="Expected engineer-days")
    parser.add_argument("--range", nargs=2, type=int, metavar=("LOW", "HIGH"), default=None)
    parser.add_argument("--expect-abstention", action="store_true")
    parser.add_argument("--difficulty", default="regression")
    parser.add_argument("--notes", default="")
    args = parser.parse_args(argv)

    transcript = (
        Path(args.transcript_file).read_text(encoding="utf-8")
        if args.transcript_file
        else sys.stdin.read()
    ).strip()

    if len(transcript) < 100:
        print("transcript must be at least 100 characters", file=sys.stderr)
        return 2
    if not args.expect_abstention and (args.expected is None or args.range is None):
        print(
            "an estimation case needs --expected and --range "
            "(or --expect-abstention). What SHOULD it have answered is the "
            "decision only you can make.",
            file=sys.stderr,
        )
        return 2

    path = Path(args.golden_set)
    cases = json.loads(path.read_text(encoding="utf-8"))
    if any(c["id"] == args.id for c in cases):
        print(f"case id {args.id!r} already exists", file=sys.stderr)
        return 2

    cases.append({
        "id": args.id,
        "difficulty": "abstention" if args.expect_abstention else args.difficulty,
        "transcript": transcript,
        "expected_engineer_days": None if args.expect_abstention else args.expected,
        "acceptable_range": None if args.expect_abstention else list(args.range),
        "expected_sources_include": [],
        "expect_abstention": bool(args.expect_abstention),
        "notes": args.notes,
    })
    path.write_text(json.dumps(cases, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Validate with the harness's own loader: a malformed capture must fail now,
    # not three weeks later in the middle of a gate run.
    sys.path.insert(0, str(EVAL_DIR.parent))
    from eval.run_eval import load_golden_set

    load_golden_set(path)
    print(f"{args.id} → {path}  ({len(cases)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
