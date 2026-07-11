"""Node-internal LLM I/O models for the graph.

These are the ``response_model``s the structured-output nodes hand to
``LLMWrapper.complete_structured`` (Instructor validates + re-prompts the LLM
against them). They are deliberately kept OUT of ``app/domain/schemas`` — that
package is the external contract with Rails; these are private plumbing of the
graph nodes. The public request/response contract lives in
``app/domain/schemas/graph_estimation.py``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Confidence = Literal["low", "medium", "high"]


class RequirementsExtraction(BaseModel):
    """Output of ``extract_requirements``: the flat list of requirements."""

    requirements: list[str] = Field(
        default_factory=list,
        description="Concrete, atomic functional/technical requirements the client "
        "wants, one per item, in concise technical English. Ignore small talk.",
    )


class ComponentModel(BaseModel):
    """One classified component (mirrors the ``Component`` TypedDict)."""

    name: str = Field(description="Short component name, e.g. 'Business backend API'.")
    category: str = Field(
        description="Coarse component category, e.g. 'backend', 'integration', "
        "'mobile', 'analytics', 'frontend', 'infrastructure'."
    )


class ComponentClassification(BaseModel):
    """Output of ``classify_components``: requirements grouped into components."""

    components: list[ComponentModel] = Field(default_factory=list)


class ComponentEstimate(BaseModel):
    """A single component's consolidated effort in the final estimate."""

    name: str
    engineer_days: int | None = Field(
        default=None,
        ge=0,
        description="Consolidated effort in engineer-days, as an INTEGER, in THIS "
        "field (not only in the rationale). Set it to the rounded median of the "
        "component's references converted to days. Use null ONLY when the component "
        "has NO references.",
    )
    rationale: str = Field(
        description="One line on how the number was derived from the references."
    )


class ConsolidatedEstimate(BaseModel):
    """Output of ``generate_estimate``: the structured estimate.

    Grounded in the ``budget_matches`` the graph accumulated (historical hours), so
    the numbers trace back to retrieved references rather than being invented.
    """

    components: list[ComponentEstimate] = Field(default_factory=list)
    total_engineer_days: int | None = Field(default=None, ge=0)
    confidence: Confidence = "medium"
    reasoning: str = Field(description="Short explanation of the consolidation.")
