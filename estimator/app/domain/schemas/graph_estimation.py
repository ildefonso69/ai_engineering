"""The public contract for the graph estimate endpoint (Session 13).

These mirror the "transcript in, structured estimate + status out" contract the
service has always exposed — the LangGraph machinery underneath is invisible to the
Rails business backend. Kept in ``domain/schemas`` (the contract layer), separate
from the node-internal LLM models in ``app/domain/graph/schemas.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.graph.state import BudgetMatch, Component


class GraphEstimateRequest(BaseModel):
    """Payload for ``POST /v1/estimate/graph``."""

    transcript: str = Field(min_length=100, max_length=50_000)
    # Used as the checkpointer ``thread_id`` so a re-run resumes the same thread.
    # Defaults to a fresh UUID in the router when omitted.
    estimation_id: str | None = Field(default=None, max_length=128)


class GraphEstimateResponse(BaseModel):
    """The graph's terminal state, surfaced as the endpoint response.

    ``estimate`` is the consolidated ``ConsolidatedEstimate`` (as a dict) and
    ``status`` is the value the ``validate_and_consolidate`` node set — the two the
    external contract cares about. The rest expose the intermediate artifacts for
    the wizard / debugging.
    """

    estimation_id: str
    status: str  # "validated" | "needs_review"
    estimate: dict | None = None
    requirements: list[str] = Field(default_factory=list)
    components: list[Component] = Field(default_factory=list)
    budget_matches: list[BudgetMatch] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
