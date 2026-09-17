"""LangGraph re-expression of the transcript-to-estimate pipeline (Session 13).

Replaces the Session 12 ad-hoc ``while`` agent loop with an explicit, sequential
``StateGraph``:

    START -> extract_requirements -> classify_components -> search_budgets
          -> generate_estimate -> validate_and_consolidate
          --[validation_passed]-----> mark_validated -----> END
          --[not validation_passed]-> mark_needs_review --> END

Node responsibilities:
    1. extract_requirements   - transcript -> discrete requirements + a
                                 structured brief (reused for retrieval filters).
    2. classify_components    - groups requirements into named components,
                                 each tagged with a category.
    3. search_budgets         - retrieves reference historical budgets for each
                                 component, ONE AFTER ANOTHER (plain ``for`` loop
                                 inside the node -- no fan-out this session).
    4. generate_estimate      - consolidates every retrieved chunk into a single
                                 grounded estimate (or short-circuits to an
                                 insufficient-context estimate, data-driven, no
                                 extra graph branch, when nothing was retrieved).
    5. validate_and_consolidate - citation validation + coherence check, folded
                                 into one node; writes a plain ``validation_passed``
                                 boolean, no status string of its own.
    6. mark_validated / mark_needs_review - trivial terminal nodes reached via a
                                 conditional edge keyed on ``validation_passed``;
                                 each only sets the final ``status`` string before
                                 ``END``. Level-3 minimal scope: just the branch
                                 and the two status values -- no retry loop,
                                 fallback node or human-in-the-loop interrupt (those
                                 are built live, not here).

Layering (see ``ARCHITECTURE.md`` Sec. 3): ``generation/<x>`` may never import a
sibling ``generation`` module (the only exception is ``agentic -> conversation``).
RAG's pipeline (reformulation, retrieval, generation, validation) therefore
cannot be imported here. Every node is a pure ``(state) -> partial_state``
closure built by :func:`build_graph` over an injected :class:`GraphDependencies`
bundle; the actual wiring of those callables to ``generation/rag/*`` lives in
the conductor (``app/domain/estimation_service.py``), the only place allowed to
know about multiple ``generation`` siblings at once. This file only deals in
plain ``dict``/``str``/``list`` shapes -- it has no notion of ``Estimate`` or
``RetrievedChunk``.

State uses a ``TypedDict`` with one accumulator reducer: ``search_results``
(``Annotated[list[dict], operator.add]``). ``search_budgets`` retrieves one
entry per component sequentially and appends to it as it goes -- a reducer
lets each partial update grow the list instead of clobbering prior components'
results, which is the natural LangGraph shape for a per-item retrieval loop
even though it runs as a single node this session. ``trace`` uses the same
reducer for the same reason: every node appends exactly one execution record.

Sequential only: no ``asyncio.gather``, no retry loops, no fallback nodes, no
human-in-the-loop interrupts, and no retries beyond what the injected
callables already implement (reused as-is from the Session 9 pipeline: one
citation retry, one coherence repair). The one conditional edge in this graph
(``validate_and_consolidate`` -> ``mark_validated``/``mark_needs_review``) is
a pure two-way terminal branch, not a loop back into the pipeline.
"""

from __future__ import annotations

import operator
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict
from uuid import uuid4

import logfire
import structlog
from langgraph.graph import END, START, StateGraph

log = structlog.get_logger()

_logfire_configured = False


def _ensure_logfire_configured() -> None:
    """Configure Logfire once, console-only when no token is set.

    The project has no dedicated Logfire setting (see ``app/config.py``); local
    console instrumentation is enough for this session's "one complete
    execution trace" deliverable, so we never require ``LOGFIRE_TOKEN``.
    """
    global _logfire_configured
    if _logfire_configured:
        return
    logfire.configure(send_to_logfire="if-token-present")
    _logfire_configured = True


class GraphState(TypedDict, total=False):
    """Shared state threaded through the graph. Plain types only (see module
    docstring on the ``agentic`` / ``rag`` layering constraint)."""

    transcript: str
    idempotency_key: str | None
    requirements: list[str]
    structured_query: dict[str, Any]
    components: list[dict[str, Any]]
    # Accumulator: one entry appended per component searched, e.g.
    # {"component": str, "chunks": [...], "low_confidence": bool}.
    search_results: Annotated[list[dict[str, Any]], operator.add]
    estimate: dict[str, Any]
    kept_chunks: list[dict[str, Any]]
    validation_passed: bool
    status: str
    trace: Annotated[list[dict[str, Any]], operator.add]


@dataclass(frozen=True)
class GraphDependencies:
    """Callables the conductor wires to the real ``generation/rag`` pipeline.

    Each callable operates on plain dicts/strings/lists so ``graph.py`` never
    has to import ``generation/rag`` types directly.
    """

    # transcript -> (requirements, structured_query)
    extract_requirements: Callable[[str], Awaitable[tuple[list[str], dict[str, Any]]]]
    # (requirements, structured_query) -> components
    classify_components: Callable[
        [list[str], dict[str, Any]], Awaitable[list[dict[str, Any]]]
    ]
    # one component -> {"component": str, "chunks": [...], "low_confidence": bool}
    search_component_budgets: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    # (structured_query, search_results) -> {"estimate": dict, "kept_chunks": [...]}
    generate_estimate: Callable[
        [dict[str, Any], list[dict[str, Any]]], Awaitable[dict[str, Any]]
    ]
    # (estimate, kept_chunks, structured_query)
    #   -> {"estimate": dict, "validation_passed": bool}
    validate_and_consolidate: Callable[
        [dict[str, Any], list[dict[str, Any]], dict[str, Any]], Awaitable[dict[str, Any]]
    ]


def _trace_entry(node_name: str, *, started_at: float, input_summary: str, output_summary: str) -> dict[str, Any]:
    return {
        "node": node_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ms": int((time.perf_counter() - started_at) * 1000),
        "input_summary": input_summary,
        "output_summary": output_summary,
    }


def build_graph(deps: GraphDependencies) -> StateGraph:
    """Build the (uncompiled) 5-node sequential StateGraph over ``deps``."""

    async def extract_requirements(state: GraphState) -> dict[str, Any]:
        started = time.perf_counter()
        with logfire.span("extract_requirements"):
            requirements, structured_query = await deps.extract_requirements(state["transcript"])
        return {
            "requirements": requirements,
            "structured_query": structured_query,
            "trace": [
                _trace_entry(
                    "extract_requirements",
                    started_at=started,
                    input_summary=f"transcript ({len(state['transcript'])} chars)",
                    output_summary=f"{len(requirements)} requirement(s)",
                )
            ],
        }

    async def classify_components(state: GraphState) -> dict[str, Any]:
        started = time.perf_counter()
        with logfire.span("classify_components"):
            components = await deps.classify_components(
                state["requirements"], state["structured_query"]
            )
        return {
            "components": components,
            "trace": [
                _trace_entry(
                    "classify_components",
                    started_at=started,
                    input_summary=f"{len(state['requirements'])} requirement(s)",
                    output_summary=f"{len(components)} component(s)",
                )
            ],
        }

    async def search_budgets(state: GraphState) -> dict[str, Any]:
        started = time.perf_counter()
        results: list[dict[str, Any]] = []
        # Sequential, one component after another (no fan-out this session).
        with logfire.span("search_budgets"):
            for component in state["components"]:
                result = await deps.search_component_budgets(component)
                results.append(result)
        total_chunks = sum(len(r.get("chunks", [])) for r in results)
        return {
            "search_results": results,
            "trace": [
                _trace_entry(
                    "search_budgets",
                    started_at=started,
                    input_summary=f"{len(state['components'])} component(s)",
                    output_summary=f"{total_chunks} chunk(s) across {len(results)} component(s)",
                )
            ],
        }

    async def generate_estimate(state: GraphState) -> dict[str, Any]:
        started = time.perf_counter()
        with logfire.span("generate_estimate"):
            outcome = await deps.generate_estimate(
                state["structured_query"], state.get("search_results", [])
            )
        return {
            "estimate": outcome["estimate"],
            "kept_chunks": outcome.get("kept_chunks", []),
            "trace": [
                _trace_entry(
                    "generate_estimate",
                    started_at=started,
                    input_summary=f"{len(state.get('search_results', []))} search result(s)",
                    output_summary=f"confidence={outcome['estimate'].get('confidence')}",
                )
            ],
        }

    async def validate_and_consolidate(state: GraphState) -> dict[str, Any]:
        started = time.perf_counter()
        with logfire.span("validate_and_consolidate"):
            outcome = await deps.validate_and_consolidate(
                state["estimate"], state.get("kept_chunks", []), state["structured_query"]
            )
        return {
            "estimate": outcome["estimate"],
            "validation_passed": outcome["validation_passed"],
            "trace": [
                _trace_entry(
                    "validate_and_consolidate",
                    started_at=started,
                    input_summary=f"confidence={state['estimate'].get('confidence')}",
                    output_summary=f"validation_passed={outcome['validation_passed']}",
                )
            ],
        }

    def _route_validation(state: GraphState) -> str:
        # Pure routing function per LangGraph convention: only reads state and
        # returns a branch key -- no side effects, no status assignment here.
        return "validated" if state["validation_passed"] else "needs_review"

    async def mark_validated(state: GraphState) -> dict[str, Any]:
        return {"status": "validated"}

    async def mark_needs_review(state: GraphState) -> dict[str, Any]:
        return {"status": "needs_review"}

    graph = StateGraph(GraphState)
    graph.add_node("extract_requirements", extract_requirements)
    graph.add_node("classify_components", classify_components)
    graph.add_node("search_budgets", search_budgets)
    graph.add_node("generate_estimate", generate_estimate)
    graph.add_node("validate_and_consolidate", validate_and_consolidate)
    graph.add_node("mark_validated", mark_validated)
    graph.add_node("mark_needs_review", mark_needs_review)

    graph.add_edge(START, "extract_requirements")
    graph.add_edge("extract_requirements", "classify_components")
    graph.add_edge("classify_components", "search_budgets")
    graph.add_edge("search_budgets", "generate_estimate")
    graph.add_edge("generate_estimate", "validate_and_consolidate")
    graph.add_conditional_edges(
        "validate_and_consolidate",
        _route_validation,
        {"validated": "mark_validated", "needs_review": "mark_needs_review"},
    )
    graph.add_edge("mark_validated", END)
    graph.add_edge("mark_needs_review", END)
    return graph


# ---------------------------------------------------------------------------
# Postgres checkpointer + compiled-graph singleton.
#
# AsyncPostgresSaver.from_conn_string(...) is an async context manager; it is
# entered once (lazily, on first run) and kept open for the process lifetime
# via an AsyncExitStack rather than re-opened per call. langgraph-checkpoint-
# postgres wants a plain ``postgresql://`` DSN, not SQLAlchemy's ``+psycopg``
# dialect suffix -- the same ``estimator-postgres`` database already used for
# pgvector/embeddings (see app/config.py::Settings.DATABASE_URL), stripped of
# that suffix. No new infrastructure is added.
# ---------------------------------------------------------------------------

_exit_stack: AsyncExitStack | None = None
_compiled_graph = None


def _checkpointer_conn_string() -> str:
    from app.config import get_settings

    settings = get_settings()
    return settings.DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")


async def _get_compiled_graph(deps: GraphDependencies):
    """Build (once) and return the compiled graph, backed by the Postgres
    checkpointer. Not thread-safe across event loops by design -- this is a
    single-process teaching service, not a hardened deployment."""
    global _exit_stack, _compiled_graph
    if _compiled_graph is not None:
        return _compiled_graph

    _ensure_logfire_configured()

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    _exit_stack = AsyncExitStack()
    checkpointer = await _exit_stack.enter_async_context(
        AsyncPostgresSaver.from_conn_string(_checkpointer_conn_string())
    )
    await checkpointer.setup()

    graph = build_graph(deps)
    _compiled_graph = graph.compile(checkpointer=checkpointer)
    return _compiled_graph


async def run_estimation_graph(
    transcript: str,
    deps: GraphDependencies,
    idempotency_key: str | None = None,
) -> GraphState:
    """Run the 5-node graph start-to-finish and return the final state.

    ``deps`` is injected by the caller (the conductor) so this module never
    has to import ``generation/rag``. The thread id (checkpointer session) is
    the idempotency key when given, else a fresh uuid per run.

    Returns the full final :class:`GraphState` -- including ``trace``, the raw
    ``estimate`` dict and ``status`` -- so the caller can both build a typed
    ``Estimate`` from ``estimate`` and inspect the complete node-by-node
    execution trace.
    """
    compiled = await _get_compiled_graph(deps)
    thread_id = idempotency_key or str(uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    initial_state: GraphState = {
        "transcript": transcript,
        "idempotency_key": idempotency_key,
        "requirements": [],
        "structured_query": {},
        "components": [],
        "search_results": [],
        "kept_chunks": [],
        "status": "pending",
        "trace": [],
    }

    log.info("estimation_graph_started", thread_id=thread_id)
    final_state = await compiled.ainvoke(initial_state, config=config)
    log.info(
        "estimation_graph_completed",
        thread_id=thread_id,
        status=final_state.get("status"),
        nodes_executed=len(final_state.get("trace", [])),
    )
    return final_state
