#!/usr/bin/env python3
"""Session 12 — run the hand-written estimation agent over a transcript.

This is the deliverable generator: it feeds a meeting transcript to the manual
agent loop (``app/generation/agentic/agent_loop.py``), prints the step-by-step
trace (reasoning → action → observation) and the final structured estimate, and
optionally writes them to a file.

Cost discipline (from the statement): debug the LOOP MECHANICS cheaply first with
``gpt-5-mini`` and the simple transcript, then switch to ``gpt-5`` / ``medium`` for
the real run on the complex transcript.

    # 1) cheap loop debugging (real retrieval, needs the stack up + task corpus)
    docker compose exec estimator python scripts/run_agent_s12.py \\
        exercises/session-12/sample_transcript_simple.txt --model gpt-5-mini --effort minimal

    # 2) offline loop debugging with the student stub (NO database needed)
    uv run python scripts/run_agent_s12.py \\
        exercises/session-12/sample_transcript_simple.txt --model gpt-5-mini --stub

    # 3) the real deliverable run
    docker compose exec estimator python scripts/run_agent_s12.py \\
        exercises/session-12/sample_transcript_complex.txt --model gpt-5 --effort medium \\
        --out exercises/session-12/example_trace_complex.txt

``search_budgets`` wraps the real S9/S10 ``retrieve()`` pipeline by default, so the
real runs need the stack up and the historical-task corpus ingested
(``scripts/build_task_corpus.py --ingest``). ``--stub`` swaps in the offline
reference retrieval so the loop can be exercised without a database.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings  # noqa: E402
from app.dependencies import get_async_openai_client  # noqa: E402
from app.generation.agentic.agent_loop import run_estimation_agent  # noqa: E402
from app.generation.agentic.agent_schemas import AgentRunResult, SearchBudgetsArgs  # noqa: E402

STUB_PATH = REPO_ROOT / "exercises" / "session-12" / "reference_retrieval.py"


def _load_stub_backend():
    """Load the student safety-net retrieval stub and adapt it to a RetrievalBackend."""
    spec = importlib.util.spec_from_file_location("s12_reference_retrieval", STUB_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Could not load stub retrieval from {STUB_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def stub_backend(args: SearchBudgetsArgs) -> list[dict]:
        filters = args.filters.model_dump() if args.filters else None
        return module.search_budgets_stub(args.query, filters)

    return stub_backend


def _render(result: AgentRunResult) -> str:
    lines = [
        "=" * 78,
        "AGENT TRACE",
        "=" * 78,
        result.trace.render(),
        "",
        "=" * 78,
        f"FINAL ESTIMATE  (iterations={result.iterations}, stopped={result.stopped_reason})",
        "=" * 78,
    ]
    estimate = result.estimate
    if estimate is None:
        lines.append("(the agent stopped without producing a structured estimate)")
        return "\n".join(lines)

    for component in estimate.components:
        cited = ", ".join(str(c) for c in component.cited_chunk_ids) or "none"
        lines.append(f"  - {component.name}: {component.estimated_hours}h  [sources: {cited}]")
        lines.append(f"      {component.rationale}")
    lines.append("")
    lines.append(f"  TOTAL: {estimate.total_hours}h    confidence: {estimate.confidence}")
    if estimate.assumptions:
        lines.append("  assumptions:")
        for assumption in estimate.assumptions:
            lines.append(f"    · {assumption}")
    return "\n".join(lines)


async def _main_async(args: argparse.Namespace) -> int:
    transcript_path = Path(args.transcript)
    if not transcript_path.is_file():
        print(f"ERROR: transcript not found: {transcript_path}", file=sys.stderr)
        return 1

    client = get_async_openai_client()
    if client is None:
        print(
            "ERROR: OPENAI_API_KEY is not set — the agent needs the OpenAI Responses API.",
            file=sys.stderr,
        )
        return 1

    backend = _load_stub_backend() if args.stub else None
    transcript = transcript_path.read_text(encoding="utf-8")

    print(f"transcript : {transcript_path}")
    print(
        f"model      : {args.model}   effort: {args.effort}   backend: "
        f"{'stub' if args.stub else 'retrieve() pipeline'}"
    )
    print()

    result = await run_estimation_agent(
        transcript,
        client=client,
        model=args.model,
        reasoning_effort=args.effort,
        max_iterations=args.max_iterations,
        retrieval_backend=backend,
    )

    rendered = _render(result)
    print(rendered)

    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
        print(f"\n(trace written to {args.out})")
    return 0


def main() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Run the Session 12 estimation agent.")
    parser.add_argument("transcript", help="Path to a meeting transcript .txt file.")
    parser.add_argument(
        "--model",
        default=settings.AGENT_MODEL,
        help=f"OpenAI model (default {settings.AGENT_MODEL}).",
    )
    parser.add_argument(
        "--effort",
        default=settings.AGENT_REASONING_EFFORT,
        choices=["minimal", "low", "medium", "high"],
        help="Reasoning effort for the Responses API.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=settings.AGENT_MAX_ITERATIONS,
        help="Loop safeguard: max Responses API round-trips.",
    )
    parser.add_argument(
        "--stub",
        action="store_true",
        help="Use the offline reference retrieval stub (no database).",
    )
    parser.add_argument("--out", help="Write the rendered trace + estimate to this file.")
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
