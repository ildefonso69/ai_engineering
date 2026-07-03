"""Schemas for the Session 12 hand-written agent.

Three families of models live here:

* **Tool argument models** (``SearchBudgetsArgs`` …) — the loop validates every
  ``json.loads(function_call.arguments)`` into one of these BEFORE dispatch, so a
  malformed / hallucinated argument becomes a returned error string the model can
  self-correct from, never an exception that kills the loop.
* **Trace models** (``AgentStep`` / ``AgentTrace``) — the reasoning→action→
  observation record the exercise requires. ``AgentTrace.render`` prints the
  ``STEP N`` console format from the statement.
* **Result models** (``AgentComponent`` / ``AgentEstimate`` / ``AgentRunResult``)
  — a deliberately LIGHT final estimate, distinct from the heavy RAG ``Estimate``
  (which mandates ``SourceCitation`` / ``WorkModule`` / coherence checks). The
  terminal ``responses.parse`` call in the loop fills ``AgentEstimate``.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

Confidence = Literal["low", "medium", "high"]

# Strip NUL and other control characters a model may occasionally emit inside a
# malformed unicode escape (e.g. a stray NUL byte in a tool argument), so a
# model glitch never poisons the readable trace / the committed deliverable.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


# --------------------------------------------------------------------------- #
# Tool argument models                                                        #
# --------------------------------------------------------------------------- #
class SearchBudgetsFilters(BaseModel):
    """Optional structural filters for a budget search.

    Mirrors the nullable ``filters`` object in the tool schema. Both fields are
    optional; ``None`` means "do not constrain on this axis".
    """

    sectors: list[str] | None = Field(
        default=None,
        description="Restrict to these client sectors (e.g. ['logistics', 'industrial']).",
    )
    component_type: str | None = Field(
        default=None,
        description="Free-text hint about the kind of component (e.g. 'mobile app').",
    )


class SearchBudgetsArgs(BaseModel):
    """Validated arguments for the ``search_budgets`` tool."""

    query: str = Field(min_length=1)
    filters: SearchBudgetsFilters | None = None


class ComponentInput(BaseModel):
    """One component the agent wants costed, with its historical references."""

    name: str = Field(min_length=1)
    reference_amounts: list[float] = Field(
        description="Historical amounts (engineer-hours) for analogous work, from search_budgets."
    )


class CalculateEstimateArgs(BaseModel):
    """Validated arguments for the ``calculate_estimate`` tool."""

    components: list[ComponentInput]


class ValidateComponentInput(BaseModel):
    """One line of the estimate to validate."""

    name: str = Field(min_length=1)
    estimated_hours: float
    reference_amounts: list[float] = Field(default_factory=list)


class ValidateEstimateArgs(BaseModel):
    """Validated arguments for the ``validate_estimate`` tool."""

    components: list[ValidateComponentInput]
    total_hours: float


# --------------------------------------------------------------------------- #
# Trace models                                                                #
# --------------------------------------------------------------------------- #
class AgentStep(BaseModel):
    """One reason→act→observe step of the loop."""

    step: int = Field(ge=1)
    reasoning_summary: str | None = Field(
        default=None,
        description="Model reasoning summary for this step (Responses API reasoning summary).",
    )
    tool: str = Field(description="Name of the invoked tool.")
    tool_args: dict[str, Any] = Field(description="Arguments the model passed to the tool.")
    observation: str = Field(description="Human-readable summary of the tool result.")


class AgentTrace(BaseModel):
    """Ordered record of everything the agent did, for auditing and the deliverable."""

    steps: list[AgentStep] = Field(default_factory=list)

    def render(self) -> str:
        """Render the trace in the ``STEP N`` console format from the statement."""
        if not self.steps:
            return "(no tool steps — the agent answered without calling any tool)"
        blocks: list[str] = []
        for s in self.steps:
            reasoning = s.reasoning_summary or "(no reasoning summary emitted)"
            args = _CONTROL_CHARS.sub("", json.dumps(s.tool_args, ensure_ascii=False, default=str))
            blocks.append(
                f"STEP {s.step}\n"
                f"  reasoning:   {reasoning}\n"
                f"  action:      {s.tool}({args})\n"
                f"  observation: {s.observation}"
            )
        return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# Result models                                                               #
# --------------------------------------------------------------------------- #
class AgentComponent(BaseModel):
    """One costed component in the final estimate."""

    name: str
    estimated_hours: float = Field(ge=0)
    cited_chunk_ids: list[int] = Field(
        default_factory=list,
        description="DB ids of the historical chunks that grounded this component.",
    )
    rationale: str = Field(description="Why this number, in one or two sentences.")


class AgentEstimate(BaseModel):
    """The agent's final structured estimate (light — no mandatory citations)."""

    components: list[AgentComponent]
    total_hours: float = Field(ge=0)
    assumptions: list[str] = Field(default_factory=list)
    confidence: Confidence


class AgentRunResult(BaseModel):
    """Everything a single agent run produces: the estimate plus its trace."""

    estimate: AgentEstimate | None = Field(
        default=None,
        description="None when the loop stopped before producing a parseable estimate.",
    )
    trace: AgentTrace
    iterations: int = Field(ge=0, description="Number of Responses API round-trips.")
    stopped_reason: Literal["completed", "max_iterations", "no_final_estimate"] = "completed"
