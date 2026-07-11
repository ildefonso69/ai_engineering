#!/usr/bin/env python3
"""Session 13 — run the estimation graph over a transcript, end to end.

Drives the compiled LangGraph ``StateGraph`` (``app/domain/graph``) sequentially:

    extract_requirements → classify_components → search_budgets
      → generate_estimate → validate_and_consolidate

and prints the terminal state (requirements → components → budget matches →
estimate → status). This is the script that produces the deliverable run/trace.

Persistence + observability (Levels 2):

* By default it opens the SAME Postgres the project uses (pgvector) as the
  checkpointer; pass ``--memory`` to use an in-process ``MemorySaver`` instead (no
  checkpointer DB — but the LLM nodes and, unless ``--stub``, retrieval still need
  their services).
* Set ``LOGFIRE_TOKEN`` in the environment to export one span per node to Pydantic
  Logfire and get the trace link. With no token the spans still run locally.

Run variants::

    # Deliverable run: real retrieval + Postgres checkpointer + Logfire
    docker compose exec estimator python scripts/run_graph_s13.py \\
        --out exercises/session-13/example_run_complex.txt

    # Partial-offline smoke: no DB, canned retrieval (still needs OPENAI_API_KEY
    # for the extract/classify/generate LLM nodes)
    uv run python scripts/run_graph_s13.py --memory --stub

``--stub`` swaps the real S9/S10 retrieval for the offline reference stub
(``exercises/session-12/reference_retrieval.py``) so ``search_budgets`` needs no
database. The real path needs the historical-task corpus ingested
(``scripts/build_task_corpus.py --ingest``).
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

from app.domain.graph.build import build_graph  # noqa: E402
from app.domain.graph.observability import configure_logfire  # noqa: E402

DEFAULT_TRANSCRIPT = REPO_ROOT / "exercises" / "session-12" / "sample_transcript_complex.txt"
STUB_PATH = REPO_ROOT / "exercises" / "session-12" / "reference_retrieval.py"


def _install_stub_backend() -> None:
    """Monkeypatch the nodes' retrieval factory with the offline reference stub."""
    spec = importlib.util.spec_from_file_location("s13_reference_retrieval", STUB_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Could not load stub retrieval from {STUB_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def _stub_backend(query: str, sectors: list[str] | None) -> list[dict]:
        filters = {"sectors": sectors, "component_type": None} if sectors else None
        return module.search_budgets_stub(query, filters)

    from app.domain.graph import nodes

    nodes.make_retrieval_backend = lambda *a, **k: _stub_backend  # type: ignore[assignment]


def _render(state: dict) -> str:
    lines = [
        "=" * 78,
        "SESSION 13 — ESTIMATION GRAPH RUN",
        "=" * 78,
        f"estimation_id : {state.get('estimation_id')}",
        f"status        : {state.get('status')}",
        "",
        "REQUIREMENTS",
    ]
    for r in state.get("requirements") or []:
        lines.append(f"  - {r}")

    lines += ["", "COMPONENTS"]
    for c in state.get("components") or []:
        lines.append(f"  - {c['name']} [{c['category']}]")

    lines += ["", "BUDGET MATCHES (accumulator field)"]
    matches = state.get("budget_matches") or []
    for m in matches:
        lines.append(
            f"  - {m['component']}: {m['amount']:.0f}h "
            f"(ref {m['reference_budget_id']}, distance {m['distance']})"
        )
    lines.append(f"  ({len(matches)} matches total)")

    lines += ["", "ESTIMATE"]
    estimate = state.get("estimate") or {}
    for c in estimate.get("components") or []:
        days = c.get("engineer_days")
        days_text = f"{days}d" if days is not None else "unbudgeted"
        lines.append(f"  - {c['name']}: {days_text} — {c.get('rationale', '')}")
    lines.append(
        f"  TOTAL: {estimate.get('total_engineer_days')}d (confidence {estimate.get('confidence')})"
    )

    errors = state.get("errors") or []
    if errors:
        lines += ["", "ERRORS / ISSUES"]
        lines += [f"  - {e}" for e in errors]
    return "\n".join(lines)


async def _run(graph, transcript: str, estimation_id: str) -> dict:
    config = {"configurable": {"thread_id": estimation_id}}
    return await graph.ainvoke({"transcript": transcript, "estimation_id": estimation_id}, config)


async def _main_async(args: argparse.Namespace) -> int:
    transcript_path = Path(args.transcript)
    if not transcript_path.is_file():
        print(f"ERROR: transcript not found: {transcript_path}", file=sys.stderr)
        return 1
    transcript = transcript_path.read_text(encoding="utf-8")
    estimation_id = args.estimation_id or f"s13-{transcript_path.stem}"

    configure_logfire()  # no FastAPI app in the CLI; spans + httpx only
    if args.stub:
        _install_stub_backend()

    print(f"transcript    : {transcript_path}")
    print(f"checkpointer  : {'MemorySaver' if args.memory else 'AsyncPostgresSaver'}")
    print(f"retrieval     : {'stub (offline)' if args.stub else 'real retrieve()'}")
    print(f"estimation_id : {estimation_id}\n")

    if args.memory:
        from langgraph.checkpoint.memory import MemorySaver

        graph = build_graph(MemorySaver())
        state = await _run(graph, transcript, estimation_id)
    else:
        from app.domain.graph.checkpointer import open_checkpointer

        async with open_checkpointer() as checkpointer:
            graph = build_graph(checkpointer)
            state = await _run(graph, transcript, estimation_id)

    rendered = _render(state)
    print(rendered)
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
        print(f"\n(run written to {args.out})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Session 13 estimation graph.")
    parser.add_argument(
        "--transcript",
        default=str(DEFAULT_TRANSCRIPT),
        help="Path to a meeting transcript .txt (default: the complex RUTA transcript).",
    )
    parser.add_argument("--estimation-id", help="thread_id for the checkpointer (default derived).")
    parser.add_argument(
        "--memory",
        action="store_true",
        help="Use an in-process MemorySaver instead of the Postgres checkpointer.",
    )
    parser.add_argument(
        "--stub",
        action="store_true",
        help="Use the offline reference retrieval stub (no database for search_budgets).",
    )
    parser.add_argument("--out", help="Write the rendered run to this file.")
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
