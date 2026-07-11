"""The graph's shared, typed state (Level 1).

A LangGraph ``StateGraph`` threads ONE state object through every node; each node
returns a *partial* update and LangGraph merges it in. For most fields the merge is
last-writer-wins (the update replaces the value). For a field annotated with a
**reducer** the merge is delegated to that reducer instead — here ``operator.add``,
so a node can return only the *new* items and LangGraph appends them to what prior
nodes accumulated.

Two accumulator fields use that pattern (the statement asks for at least one):

* ``budget_matches`` — ``search_budgets`` searches each component one at a time and
  returns only that component's matches; the reducer grows the list across the loop
  (and, once the live session parallelises the search, across concurrent branches).
* ``errors`` — any node can append a soft failure without clobbering earlier ones.
"""

from __future__ import annotations

import operator
from typing import Annotated, Optional

# Pydantic (used by the response model that embeds Component/BudgetMatch) requires
# typing_extensions.TypedDict on Python < 3.12; LangGraph accepts it too.
from typing_extensions import TypedDict


class Component(TypedDict):
    """One functional component the project decomposes into."""

    name: str
    category: str


class BudgetMatch(TypedDict):
    """A historical reference budget retrieved for a component.

    ``amount`` carries the matched historical item's recorded engineer-hours (the
    grounding number the estimate is built from); ``distance`` is its cosine
    distance from the query (lower = closer).
    """

    component: str
    reference_budget_id: Optional[str]
    amount: float
    distance: float


class EstimationState(TypedDict, total=False):
    """The state threaded through the graph.

    ``total=False`` so a node may return a partial dict without every key present;
    the initial invoke only supplies ``transcript`` (+ ``estimation_id``).
    """

    transcript: str
    estimation_id: str
    requirements: list[str]
    components: list[Component]
    # Accumulator: grows as each component is searched (reducer = list concat).
    budget_matches: Annotated[list[BudgetMatch], operator.add]
    estimate: Optional[dict]
    status: Optional[str]  # "validated" | "needs_review"
    # Accumulator: soft failures appended by any node, never clobbered.
    errors: Annotated[list[str], operator.add]
