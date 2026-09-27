"""Supervisor/workers multi-agent topology for transcript-to-estimate (Session 14).

Re-expresses Session 13's LINEAR 5-node graph (``graph.py``, left untouched) as
a supervisor/workers topology: one hand-built ``supervisor`` node routes to
five specialist agents via ``langgraph.types.Command``, instead of a fixed
node order. Every routing decision is therefore an explicit
``Command(goto=..., update={...})`` -- visible in the execution trace -- and
NOT hidden inside the prebuilt ``create_supervisor`` helper (deliberately not
used here).

"Level 1/2/3" in the comments throughout this module refer to this session's
own three-tier assignment rubric, in increasing order of sophistication:

    Level 1 -- supervisor/workers structure (the dynamic ``Command`` routing,
               the ``budget_matches``/``trace`` accumulator reducers).
    Level 2 -- human-review triggers (``human_review_gate``'s low_confidence /
               no_historical_precedent / outside_historical_range gates).
    Level 3 -- privilege + audit (``AGENT_TOOL_PRIVILEGES``,
               ``_authorize_tool_call``/``_log_tool_result``).

Tool-privilege contract (the actual point of the Level 3 exercise -- enforced
by construction: each specialist closure only closes over the ONE callable it
is allowed to use):

    requirements_extractor -> no business tool (LLM-only decomposition)
    budget_searcher        -> search_component_budgets (wraps S9/S10 retrieval)
    estimate_generator     -> calculate_estimate (deterministic, no LLM)
    coherence_validator    -> validate_estimate (citations + coherence)
    human_review_gate      -> no business tool; interrupt() is logged separately
    finalize               -> no business tool; folds human_decision into status
    supervisor             -> no tool, routing only

Topology (7 nodes total -- the four specialists, the routing supervisor, and
the Level 2 human-in-the-loop tail):

    START -> supervisor -> {requirements_extractor, budget_searcher,
                             estimate_generator, coherence_validator} -> supervisor
                             -> ... -> supervisor
                             -> human_review_gate -> finalize -> END

Every specialist (requirements_extractor, budget_searcher, estimate_generator,
coherence_validator) edges back to "supervisor" via a plain ``add_edge`` --
they are pure ``(state) -> partial_state`` functions, no ``Command`` of their
own. Only "supervisor" carries dynamic routing, expressed via the ``Command``
it returns -- LangGraph dispatches on that ``Command.goto`` directly, so no
``add_conditional_edges`` is needed for it. Once ``validation`` is set,
"supervisor" always hands off to "human_review_gate" (never back to itself);
"human_review_gate" either pauses (``interrupt()``, see its own docstring) or
falls through to "finalize" via a static ``add_edge``, and "finalize" is the
only node with a plain edge to ``END``.

Layering (``ARCHITECTURE.md`` Sec. 3, same rule ``graph.py`` already follows):
``generation/agentic`` may never import sibling ``generation/rag`` directly.
This module only deals in plain ``dict``/``str``/``list`` shapes; the actual
wiring of the four dependency callables to ``generation/rag/*`` happens in the
conductor (``app/domain/estimation_service.py``).

State: ``SupervisorState`` extends Session 13's ``GraphState`` shape.
``budget_matches`` is the one mandatory accumulator reducer (Level 1
requirement) -- each ``budget_searcher`` visit appends its component results
rather than clobbering prior ones. ``trace`` reuses the same reducer pattern
as ``graph.py`` so every node AND every supervisor decision leaves one record.
"""

from __future__ import annotations

import operator
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypedDict
from uuid import uuid4

import logfire
import structlog
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

log = structlog.get_logger()

_logfire_configured = False


class ToolPrivilegeViolation(Exception):
    """Raised when a node attempts to call a tool outside its declared privilege set.

    Kept local to ``generation/agentic`` (not a sibling of ``RagError`` in
    ``generation/rag/errors.py``) because ``generation/<x>`` may never import
    another ``generation`` sibling (``ARCHITECTURE.md`` Sec. 3).
    """


class GraphNotFoundError(Exception):
    """No checkpoint exists at all for the given ``thread_id``.

    Raised by :func:`resume_supervisor_graph` when the checkpointer has never
    seen this ``thread_id`` -- there is nothing to resume. Kept local to
    ``generation/agentic`` for the same layering reason as
    :class:`ToolPrivilegeViolation`.
    """


class GraphNotPausedError(Exception):
    """A checkpoint exists for ``thread_id`` but there is no pending interrupt.

    Covers both "already resumed/completed" and "never paused" -- either way,
    ``Command(resume=...)`` has nothing to feed. Raised by
    :func:`resume_supervisor_graph`.
    """


# Level 3 (explicit, auditable privilege gate -- IN ADDITION to the structural
# enforcement already provided by ``SupervisorGraphDependencies``, where each
# specialist closure only ever sees the one callable it is allowed to use).
# Agent name -> the tool name(s) it is allowed to invoke.
AGENT_TOOL_PRIVILEGES: dict[str, frozenset[str]] = {
    "requirements_extractor": frozenset(),  # LLM-only decomposition, no business tool
    "budget_searcher": frozenset({"search_component_budgets"}),
    "estimate_generator": frozenset({"calculate_estimate"}),
    "coherence_validator": frozenset({"validate_estimate"}),
    "supervisor": frozenset(),  # routing only
    "human_review_gate": frozenset(),  # no business tool; interrupt() is logged separately
    "finalize": frozenset(),  # no business tool; folds human_decision into state/status only
}


def _summarize_for_log(value: Any, max_len: int = 200) -> Any:
    """Truncate/summarize a value for structlog, same convention as ``_trace_entry``
    (counts instead of full lists, sliced strings) -- never dump raw chunk content."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= max_len else value[:max_len] + "..."
    if isinstance(value, dict):
        return f"dict[{len(value)}]: {list(value.keys())[:10]}"
    if isinstance(value, (list, tuple, set, frozenset)):
        return f"{type(value).__name__}[{len(value)}]"
    return str(value)[:max_len]


def _authorize_tool_call(agent_name: str, tool_name: str, tool_input: Any) -> None:
    """Explicit, auditable runtime gate: is ``tool_name`` in ``agent_name``'s
    declared privilege set? Logs the decision either way, and RAISES on denial
    so a violation can never be silently swallowed -- see module docstring/spec.
    """
    input_summary = _summarize_for_log(tool_input)
    allowed = AGENT_TOOL_PRIVILEGES.get(agent_name, frozenset())
    if tool_name not in allowed:
        log.error(
            "agent_tool_call_denied",
            agent=agent_name,
            tool=tool_name,
            input=input_summary,
            status="denied",
        )
        raise ToolPrivilegeViolation(
            f"agent '{agent_name}' is not privileged to call tool '{tool_name}'"
        )
    log.info(
        "agent_tool_call",
        agent=agent_name,
        tool=tool_name,
        input=input_summary,
        status="authorized",
    )


def _log_tool_result(agent_name: str, tool_name: str, result: Any) -> None:
    """Second half of the audit trail: what the authorized call actually returned."""
    log.info(
        "agent_tool_result",
        agent=agent_name,
        tool=tool_name,
        result_summary=_summarize_for_log(result),
    )


def _ensure_logfire_configured() -> None:
    """Configure Logfire once, console-only when no token is set (same as ``graph.py``)."""
    global _logfire_configured
    if _logfire_configured:
        return
    logfire.configure(send_to_logfire="if-token-present")
    _logfire_configured = True


class SupervisorState(TypedDict, total=False):
    """Shared state threaded through the supervisor graph. Plain types only."""

    transcript: str
    idempotency_key: str | None
    # Checkpointer thread_id actually used for this run (== idempotency_key
    # when the caller supplied one, else the generated uuid) -- surfaced so
    # the conductor can hand it back as ``estimation_id`` when the graph
    # pauses and the caller gave no idempotency_key of their own.
    thread_id: str
    requirements: list[str]
    structured_query: dict[str, Any]
    components: list[dict[str, Any]]
    # Accumulator: one entry appended per budget_searcher visit, e.g.
    # {"component": str, "chunks": [...], "low_confidence": bool}.
    budget_matches: Annotated[list[dict[str, Any]], operator.add]
    estimate: dict[str, Any] | None
    validation: dict[str, Any] | None
    confidence: float | None
    status: str
    # Session 14: the human's verdict once ``human_review_gate`` resumes (None
    # when the gate never paused -- e.g. {"action": "approve"|"adjust"|"reject", ...}).
    human_decision: dict[str, Any] | None
    trace: Annotated[list[dict[str, Any]], operator.add]


@dataclass(frozen=True)
class SupervisorGraphDependencies:
    """Callables the conductor wires to the real ``generation/rag`` tools.

    Exactly one callable per specialist agent -- this IS the tool-privilege
    table, enforced structurally: a node's closure only ever sees the one
    callable it is meant to use.
    """

    # transcript -> {"requirements": [...], "structured_query": {...}, "components": [...]}
    extract_requirements: Callable[[str], Awaitable[dict[str, Any]]]
    # one component -> {"component": str, "chunks": [...], "low_confidence": bool}
    search_component_budgets: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    # list of {"name": str, "reference_amounts": list[int]} -> deterministic aggregation
    # (agent_tools.calculate_estimate, reused as-is -- sync, no LLM).
    calculate_estimate: Callable[[list[dict[str, Any]]], dict[str, Any]]
    # (estimate, kept_chunks, structured_query) -> {"estimate", "validation_passed", "confidence"}
    validate_estimate: Callable[
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


# Matches the literal text the structural chunker writes for each historical
# task/component ("Estimated hours: <N>") -- see
# app/generation/rag/chunking/structural.py. Parsing it here (plain text, no
# schema import) is how estimate_generator grounds calculate_estimate's
# deterministic aggregation in the actually-retrieved historical numbers.
_HOURS_RE = re.compile(r"Estimated hours:\s*(\d+)", re.IGNORECASE)


def _no_historical_precedent(budget_matches: list[dict[str, Any]]) -> bool:
    """True when the transcript has no usable precedent in the budget corpus.

    "Usable" reuses the signal ``budget_searcher``/S12 already computes per
    component (``low_confidence`` = soft-fail, or zero chunks) rather than
    inventing a new heuristic -- if every component came back empty or
    soft-failed, there is nothing to ground the estimate in.
    """
    if not budget_matches:
        return True
    return all(not m.get("chunks") or m.get("low_confidence") for m in budget_matches)


def _outside_historical_range(estimate: dict[str, Any], budget_matches: list[dict[str, Any]]) -> bool:
    """True when ``total_engineer_days`` falls outside a margin around the
    historical range parsed from the retrieved chunks (same ``_HOURS_RE``
    ``estimate_generator`` already uses, hours -> engineer-days at 8h/day).

    Margin is deliberately simple and generous (half the min, double the
    max) -- this is a coarse plausibility gate for the human, not a
    statistical outlier test.
    """
    total_days = estimate.get("total_engineer_days")
    if total_days is None:
        return False  # covered separately by the low_confidence/no-precedent triggers
    historical_days = [
        int(hours) / 8
        for match in budget_matches
        for chunk in match.get("chunks", [])
        for hours in _HOURS_RE.findall(chunk.get("content", ""))
    ]
    if not historical_days:
        return False
    low, high = min(historical_days) * 0.5, max(historical_days) * 2.0
    return not (low <= total_days <= high)


def _review_trigger_reasons(state: SupervisorState, threshold: float) -> list[str]:
    """OR-combine the three Level 2 trigger signals; empty == no pause needed."""
    reasons: list[str] = []
    confidence = state.get("confidence")
    if confidence is not None and confidence < threshold:
        reasons.append("low_confidence")
    budget_matches = state.get("budget_matches", [])
    if _no_historical_precedent(budget_matches):
        reasons.append("no_historical_precedent")
    elif _outside_historical_range(state.get("estimate") or {}, budget_matches):
        reasons.append("outside_historical_range")
    return reasons


def build_supervisor_graph(deps: SupervisorGraphDependencies) -> StateGraph:
    """Build the (uncompiled) supervisor/workers StateGraph over ``deps``."""

    async def requirements_extractor(state: SupervisorState) -> dict[str, Any]:
        # Tool: none. LLM-only decomposition (transcript -> requirements +
        # components); no business/side-effecting callable is invoked here.
        started = time.perf_counter()
        log.info("agent_tool_call", agent="requirements_extractor", tool=None, status="no_tool_used")
        with logfire.span("requirements_extractor"):
            outcome = await deps.extract_requirements(state["transcript"])
        requirements = outcome.get("requirements", [])
        components = outcome.get("components", [])
        return {
            "requirements": requirements,
            "structured_query": outcome.get("structured_query", {}),
            "components": components,
            "trace": [
                _trace_entry(
                    "requirements_extractor",
                    started_at=started,
                    input_summary=f"transcript ({len(state['transcript'])} chars)",
                    output_summary=f"{len(requirements)} requirement(s), {len(components)} component(s)",
                )
            ],
        }

    async def budget_searcher(state: SupervisorState) -> dict[str, Any]:
        # Tool: search_component_budgets only.
        started = time.perf_counter()
        components = state.get("components", [])
        already_searched = {m.get("component") for m in state.get("budget_matches", [])}
        pending = [c for c in components if c.get("name") not in already_searched]
        results: list[dict[str, Any]] = []
        with logfire.span("budget_searcher"):
            for component in pending:
                _authorize_tool_call("budget_searcher", "search_component_budgets", component)
                result = await deps.search_component_budgets(component)
                _log_tool_result("budget_searcher", "search_component_budgets", result)
                results.append(result)
        total_chunks = sum(len(r.get("chunks", [])) for r in results)
        return {
            "budget_matches": results,
            "trace": [
                _trace_entry(
                    "budget_searcher",
                    started_at=started,
                    input_summary=f"{len(pending)} pending component(s)",
                    output_summary=f"{total_chunks} chunk(s) across {len(results)} component(s)",
                )
            ],
        }

    async def estimate_generator(state: SupervisorState) -> dict[str, Any]:
        # Tool: calculate_estimate only -- deterministic, no LLM. Reference
        # amounts come straight from the retrieved chunks' text via _HOURS_RE.
        started = time.perf_counter()
        budget_matches = state.get("budget_matches", [])
        components_for_tool: list[dict[str, Any]] = []
        sources_by_component: dict[str, list[int]] = {}
        for match in budget_matches:
            component_name = match.get("component", "unknown")
            chunk_ids: list[int] = []
            amounts: list[int] = []
            for chunk in match.get("chunks", []):
                chunk_ids.append(chunk["chunk_id"])
                amounts.extend(int(h) for h in _HOURS_RE.findall(chunk.get("content", "")))
            components_for_tool.append({"name": component_name, "reference_amounts": amounts})
            sources_by_component[component_name] = chunk_ids

        with logfire.span("estimate_generator"):
            _authorize_tool_call("estimate_generator", "calculate_estimate", components_for_tool)
            outcome = deps.calculate_estimate(components_for_tool)
            _log_tool_result("estimate_generator", "calculate_estimate", outcome)

        breakdown = outcome.get("breakdown", [])
        total_days = outcome.get("total_engineer_days", 0)

        if not breakdown or not total_days:
            estimate = {
                "total_engineer_days": None,
                "modules": [],
                "duration_weeks": None,
                "sources": [],
                "assumptions": [],
                "confidence": "insufficient",
                "reasoning": outcome.get("summary", "No historical data available."),
                "insufficient_context_explanation": (
                    "No parseable historical hours were found in the retrieved budgets."
                ),
            }
        else:
            modules = []
            sources = []
            for item in breakdown:
                component_name = item.get("component", "unknown")
                chunk_ids = sources_by_component.get(component_name, [])
                modules.append(
                    {
                        "name": component_name,
                        "description": None,
                        "tasks": [
                            {
                                "name": component_name,
                                "description": None,
                                "engineer_days": item.get("estimated_days", 0),
                                "sources": chunk_ids,
                            }
                        ],
                    }
                )
                sources.extend(
                    {
                        "source_id": chunk_id,
                        "relevance": "supporting",
                        "used_for": f"historical reference for {component_name}",
                    }
                    for chunk_id in chunk_ids
                )
            overall_confidence = (
                "low" if any(item.get("confidence") == "low" for item in breakdown) else "medium"
            )
            estimate = {
                "total_engineer_days": total_days,
                "modules": modules,
                "duration_weeks": None,
                "sources": sources,
                "assumptions": [],
                "confidence": overall_confidence,
                "reasoning": outcome.get("summary", ""),
                "insufficient_context_explanation": None,
            }

        return {
            "estimate": estimate,
            "trace": [
                _trace_entry(
                    "estimate_generator",
                    started_at=started,
                    input_summary=f"{len(budget_matches)} budget match(es)",
                    output_summary=f"total_engineer_days={estimate['total_engineer_days']}",
                )
            ],
        }

    async def coherence_validator(state: SupervisorState) -> dict[str, Any]:
        # Tool: validate_estimate only.
        started = time.perf_counter()
        kept_chunks = [
            chunk for match in state.get("budget_matches", []) for chunk in match.get("chunks", [])
        ]
        with logfire.span("coherence_validator"):
            validator_input = {"estimate": state["estimate"], "kept_chunks_count": len(kept_chunks)}
            _authorize_tool_call("coherence_validator", "validate_estimate", validator_input)
            outcome = await deps.validate_estimate(
                state["estimate"], kept_chunks, state.get("structured_query", {})
            )
            _log_tool_result("coherence_validator", "validate_estimate", outcome)
        return {
            "estimate": outcome["estimate"],
            "validation": {"validation_passed": outcome["validation_passed"]},
            "confidence": outcome.get("confidence"),
            "trace": [
                _trace_entry(
                    "coherence_validator",
                    started_at=started,
                    input_summary=f"{len(kept_chunks)} kept chunk(s)",
                    output_summary=f"validation_passed={outcome['validation_passed']}",
                )
            ],
        }

    async def human_review_gate(state: SupervisorState) -> dict[str, Any]:
        # Tool: none -- reads state + the configurable threshold only.
        started = time.perf_counter()
        from app.config import get_settings

        threshold = get_settings().HUMAN_REVIEW_CONFIDENCE_THRESHOLD
        reasons = _review_trigger_reasons(state, threshold)
        if not reasons:
            return {
                "trace": [
                    _trace_entry(
                        "human_review_gate",
                        started_at=started,
                        input_summary=f"confidence={state.get('confidence')}",
                        output_summary="no trigger fired, skipping review",
                    )
                ]
            }

        # interrupt() raises GraphInterrupt the FIRST time this node reaches
        # this line for a given task (no resume value recorded yet in the
        # checkpoint) -- that exception propagates out of ``ainvoke``, which
        # returns with an "__interrupt__" key instead of a finished state, and
        # nothing after this line runs. Resuming with
        # ``ainvoke(Command(resume=decision), config=config)`` re-executes
        # THIS NODE FROM THE TOP: ``reasons`` is recomputed (same state, same
        # result), and this second time interrupt() finds the resume value
        # already in the checkpoint's scratchpad and returns it instead of
        # raising, so ``decision`` below IS the human's verdict. The Postgres
        # checkpointer makes this durable across process restarts too --
        # nothing here depends on the process that paused still being alive.
        log.info(
            "agent_interrupt_call",
            agent="human_review_gate",
            action="interrupt",
            reasons=reasons,
            status="interrupt_requested",
        )
        decision = interrupt(
            {
                "reasons": reasons,
                "estimate": state.get("estimate"),
                "confidence": state.get("confidence"),
            }
        )
        return {
            "human_decision": decision,
            "trace": [
                _trace_entry(
                    "human_review_gate",
                    started_at=started,
                    input_summary=f"triggered: {reasons}",
                    output_summary=f"decision={decision}",
                )
            ],
        }

    async def finalize(state: SupervisorState) -> dict[str, Any]:
        # Tool: none. Last node before END -- folds the human's decision (if
        # any) into the final estimate/status, or falls back to the plain
        # validation_passed logic when the gate never paused.
        started = time.perf_counter()
        decision = state.get("human_decision")
        estimate = dict(state.get("estimate") or {})
        validation = state.get("validation") or {}

        if decision is None:
            status = "validated" if validation.get("validation_passed") else "needs_review"
        else:
            action = decision.get("action")
            if action == "reject":
                status = "rejected"
            elif action == "adjust":
                # The HTTP boundary (``HumanReviewDecision`` model_validator in
                # schemas.py) already rejects action="adjust" with a missing
                # value before it ever reaches the graph -- but ``decision`` is
                # a plain dict at this layer (fed back through
                # ``interrupt()``/``Command(resume=...)``, not re-validated by
                # Pydantic), so this stays defensive-in-depth. Either way, a
                # missing/implausible adjustment must NOT silently report
                # status="validated" as if the number had actually changed.
                adjusted = decision.get("adjusted_total_engineer_days")
                budget_matches = state.get("budget_matches", [])
                if adjusted is None:
                    status = "needs_review"
                    log.warning(
                        "adjust_decision_rejected",
                        thread_id=state.get("thread_id"),
                        reason="missing_adjusted_total_engineer_days",
                    )
                elif _outside_historical_range({"total_engineer_days": adjusted}, budget_matches):
                    status = "needs_review"
                    log.warning(
                        "adjust_decision_rejected",
                        thread_id=state.get("thread_id"),
                        reason="adjusted_value_outside_historical_range",
                        adjusted_total_engineer_days=adjusted,
                    )
                else:
                    estimate["total_engineer_days"] = adjusted
                    status = "validated"
            else:  # "approve"
                status = "validated"

        return {
            "estimate": estimate,
            "status": status,
            "trace": [
                _trace_entry(
                    "finalize",
                    started_at=started,
                    input_summary=f"human_decision={decision}",
                    output_summary=f"status={status}",
                )
            ],
        }

    def supervisor(
        state: SupervisorState,
    ) -> Command[
        Literal[
            "requirements_extractor",
            "budget_searcher",
            "estimate_generator",
            "coherence_validator",
            "human_review_gate",
        ]
    ]:
        # Plain routing function: no side effects beyond deciding "goto" and
        # appending one trace entry -- this is the only node whose next hop
        # is dynamic, expressed as a Command instead of add_conditional_edges.
        # Once validation is set, control always hands off to
        # human_review_gate -> finalize -> END (a static tail, like graph.py's
        # mark_validated/mark_needs_review) -- it never comes back here, so
        # END is no longer a direct supervisor destination.
        if not state.get("requirements"):
            goto: str = "requirements_extractor"
        elif state.get("components") and not state.get("budget_matches"):
            goto = "budget_searcher"
        elif state.get("estimate") is None:
            goto = "estimate_generator"
        elif state.get("validation") is None:
            goto = "coherence_validator"
        else:
            goto = "human_review_gate"

        log.info(
            "supervisor_decision",
            goto=goto,
            components_count=len(state.get("components", [])),
            budget_matches_count=len(state.get("budget_matches", [])),
            has_estimate=state.get("estimate") is not None,
            has_validation=state.get("validation") is not None,
        )
        update: dict[str, Any] = {
            "trace": [
                {
                    "node": "supervisor",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "decision": goto,
                }
            ]
        }
        return Command(goto=goto, update=update)  # type: ignore[arg-type]

    graph = StateGraph(SupervisorState)
    graph.add_node(
        "supervisor",
        supervisor,
        destinations=(
            "requirements_extractor",
            "budget_searcher",
            "estimate_generator",
            "coherence_validator",
            "human_review_gate",
        ),
    )
    graph.add_node("requirements_extractor", requirements_extractor)
    graph.add_node("budget_searcher", budget_searcher)
    graph.add_node("estimate_generator", estimate_generator)
    graph.add_node("coherence_validator", coherence_validator)
    graph.add_node("human_review_gate", human_review_gate)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "supervisor")
    # Every specialist edges back to the supervisor -- only the supervisor's
    # outgoing edge is dynamic (via the Command it returns).
    graph.add_edge("requirements_extractor", "supervisor")
    graph.add_edge("budget_searcher", "supervisor")
    graph.add_edge("estimate_generator", "supervisor")
    graph.add_edge("coherence_validator", "supervisor")
    # human_review_gate's own "goto" is not dynamic: it either pauses (raises,
    # halting the graph entirely -- see the interrupt() comment above) or
    # returns normally, and either way the only next hop once it DOES return
    # is "finalize". A plain add_edge is therefore the idiomatic equivalent of
    # the reference sketch's conditional Command(goto="finalize", ...) --
    # no add_conditional_edges/Command needed for a single fixed destination.
    graph.add_edge("human_review_gate", "finalize")
    graph.add_edge("finalize", END)
    return graph


# ---------------------------------------------------------------------------
# Postgres checkpointer + compiled-graph singleton (same mechanics as
# graph.py's; duplicated rather than factored out to avoid touching a
# delivered Session 13 artifact).
# ---------------------------------------------------------------------------

_exit_stack: AsyncExitStack | None = None
_compiled_graph = None


def _checkpointer_conn_string() -> str:
    from app.config import get_settings

    settings = get_settings()
    return settings.DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")


async def _get_compiled_graph(deps: SupervisorGraphDependencies):
    """Build (once) and return the compiled graph, backed by the Postgres checkpointer."""
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

    graph = build_supervisor_graph(deps)
    _compiled_graph = graph.compile(checkpointer=checkpointer)
    return _compiled_graph


async def run_supervisor_graph(
    transcript: str,
    deps: SupervisorGraphDependencies,
    idempotency_key: str | None = None,
) -> SupervisorState:
    """Run the supervisor/workers graph start-to-finish and return the final state."""
    compiled = await _get_compiled_graph(deps)
    thread_id = idempotency_key or str(uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    initial_state: SupervisorState = {
        "transcript": transcript,
        "idempotency_key": idempotency_key,
        "thread_id": thread_id,
        "requirements": [],
        "structured_query": {},
        "components": [],
        "budget_matches": [],
        "estimate": None,
        "validation": None,
        "confidence": None,
        "status": "pending",
        "human_decision": None,
        "trace": [],
    }

    log.info("supervisor_graph_started", thread_id=thread_id)
    final_state = await compiled.ainvoke(initial_state, config=config)
    if final_state.get("__interrupt__"):
        log.info("supervisor_graph_paused", thread_id=thread_id)
    else:
        log.info(
            "supervisor_graph_completed",
            thread_id=thread_id,
            status=final_state.get("status"),
            nodes_executed=len(final_state.get("trace", [])),
        )
    return final_state


async def resume_supervisor_graph(
    thread_id: str,
    decision: dict[str, Any],
    deps: SupervisorGraphDependencies,
) -> SupervisorState:
    """Resume a paused ``human_review_gate`` interrupt and run to completion.

    ``Command(resume=decision)`` (not a plain state dict) is what tells
    LangGraph "continue the paused task, feeding ``decision`` back to the
    ``interrupt()`` call that is waiting for it" -- see the mechanics comment
    on ``human_review_gate``. The Postgres checkpointer means this works even
    if the process that returned the pause is not the one resuming it.

    Before calling ``Command(resume=...)`` this explicitly inspects the
    checkpoint via ``aget_state`` -- ``SupervisorState`` is ``total=False``
    and several nodes (e.g. ``requirements_extractor``) do direct key access
    (``state["transcript"]``), so blindly resuming a ``thread_id`` with no
    checkpoint, or one that already ran to completion, could ``KeyError``
    deep inside the graph and surface as an unhandled crash instead of a
    clear error:

    * no checkpoint at all for ``thread_id`` (``created_at is None``) ->
      :class:`GraphNotFoundError`.
    * a checkpoint exists but ``interrupts`` is empty (already
      completed/resumed, or never paused) -> :class:`GraphNotPausedError`.
    """
    compiled = await _get_compiled_graph(deps)
    config = {"configurable": {"thread_id": thread_id}}

    snapshot = await compiled.aget_state(config)
    if snapshot.created_at is None:
        log.warning("supervisor_graph_resume_not_found", thread_id=thread_id)
        raise GraphNotFoundError(f"no checkpoint found for thread_id '{thread_id}'")
    if not snapshot.interrupts:
        log.warning("supervisor_graph_resume_not_paused", thread_id=thread_id)
        raise GraphNotPausedError(
            f"thread_id '{thread_id}' has no pending human review to resume "
            "(already completed, or never paused)"
        )

    log.info("supervisor_graph_resumed", thread_id=thread_id)
    final_state = await compiled.ainvoke(Command(resume=decision), config=config)
    log.info(
        "supervisor_graph_completed",
        thread_id=thread_id,
        status=final_state.get("status"),
        nodes_executed=len(final_state.get("trace", [])),
    )
    return final_state
