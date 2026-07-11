"""Wire and compile the estimation graph (Level 1 + Level 3).

Five nodes, sequential for now (the live session parallelises ``search_budgets``
with the Send API):

    START → extract_requirements → classify_components → search_budgets
          → generate_estimate → validate_and_consolidate → (conditional) → END

**Level 3** — the fixed ``validate_and_consolidate → END`` edge is replaced by a
CONDITIONAL edge: ``route_on_status`` reads the ``status`` the validation node set
and routes ``"validated"`` vs ``"needs_review"``. Both currently terminate at END
(the serious retry / fallback / HITL branches are built live); the routing already
makes the branch explicit. Swap ``add_conditional_edges`` for
``builder.add_edge("validate_and_consolidate", END)`` to get the plain Level-1 form.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.domain.graph.nodes import (
    classify_components,
    extract_requirements,
    generate_estimate,
    search_budgets,
    validate_and_consolidate,
)
from app.domain.graph.state import EstimationState


def route_on_status(state: EstimationState) -> str:
    """Conditional-edge router: branch on the validation ``status`` (Level 3)."""
    return "needs_review" if state.get("status") == "needs_review" else "validated"


def build_graph(checkpointer=None):
    """Build and compile the estimation graph.

    ``checkpointer`` persists state per ``thread_id`` (an ``AsyncPostgresSaver`` in
    the app, a ``MemorySaver`` in tests). ``None`` compiles a checkpointer-less
    graph — still runnable, just not resumable.
    """
    builder = StateGraph(EstimationState)

    builder.add_node("extract_requirements", extract_requirements)
    builder.add_node("classify_components", classify_components)
    builder.add_node("search_budgets", search_budgets)  # sequential for now
    builder.add_node("generate_estimate", generate_estimate)
    builder.add_node("validate_and_consolidate", validate_and_consolidate)

    builder.add_edge(START, "extract_requirements")
    builder.add_edge("extract_requirements", "classify_components")
    builder.add_edge("classify_components", "search_budgets")
    builder.add_edge("search_budgets", "generate_estimate")
    builder.add_edge("generate_estimate", "validate_and_consolidate")
    # Level 3: conditional exit keyed on the validation status.
    builder.add_conditional_edges(
        "validate_and_consolidate",
        route_on_status,
        {"validated": END, "needs_review": END},
    )

    return builder.compile(checkpointer=checkpointer)
