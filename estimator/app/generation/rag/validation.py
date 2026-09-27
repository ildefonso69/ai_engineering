"""Post-generation checks for grounded estimates (Session 9).

Two independent guards run after the LLM returns an :class:`Estimate`:

* :func:`validate_citations` — every cited ``source_id`` must correspond to a
  chunk that was actually retrieved. Fabricated ids are the classic grounding
  failure and trigger one corrective retry in the orchestrator.
* :func:`check_coherence` — the ``insufficient`` confidence level has a strict
  shape (no numbers, an explanation present); a violation is a malformed
  response, not a valid estimate.
"""

from __future__ import annotations

from typing import Any

from app.generation.rag.schemas import Estimate, EstimationQuery, RetrievedChunk

_CONFIDENCE_SCORES: dict[str, float] = {
    "high": 0.9,
    "medium": 0.6,
    "low": 0.3,
    "insufficient": 0.0,
}


def validate_citations(
    estimate: Estimate,
    retrieved_chunks: list[RetrievedChunk],
) -> list[int]:
    """Return the cited source ids that were never retrieved (fabricated).

    Checks both the top-level ``sources`` citations and the per-task
    ``modules[].tasks[].sources``. An empty list means every citation is valid
    (including the edge case of an estimate that cites nothing at all).

    Parameters
    ----------
    estimate:
        The generated estimate to inspect.
    retrieved_chunks:
        The chunks the estimate was supposed to be grounded in.

    Returns
    -------
    list[int]
        Sorted, de-duplicated fabricated source ids (empty if all valid).
    """
    valid_ids = {chunk.id for chunk in retrieved_chunks}

    cited_ids: set[int] = {citation.source_id for citation in estimate.sources}
    for module in estimate.modules:
        for task in module.tasks:
            cited_ids.update(task.sources)

    return sorted(cited_ids - valid_ids)


def check_coherence(estimate: Estimate) -> bool:
    """Return whether the estimate's confidence level matches its content.

    When ``confidence == "insufficient"``: both numeric totals must be ``None``,
    ``modules`` must be empty, and ``insufficient_context_explanation`` must be
    non-empty. Any other confidence level is always considered coherent here
    (the numeric checks belong to the schema/business rules, not to this guard).
    """
    if estimate.confidence != "insufficient":
        return True
    return (
        estimate.total_engineer_days is None
        and estimate.duration_weeks is None
        and not estimate.modules
        and bool(estimate.insufficient_context_explanation)
    )


async def validate_estimate(
    estimate: Estimate,
    kept_chunks: list[RetrievedChunk],
    structured_query: EstimationQuery,
) -> dict[str, Any]:
    """Single-callable wrapper for the ``coherence_validator`` agent's one tool.

    Reuses :func:`validate_citations` + :func:`check_coherence` with the same
    one-citation-retry, one-coherence-repair budget already proven by the
    linear graph's ``validate_and_consolidate`` node (Session 13) -- no new
    behavior, just packaged as a single function so ``coherence_validator``
    has exactly one tool to call, per the Session 14 privilege table.

    The corrective retry calls ``estimator._generate`` directly (lazy import:
    ``estimator.py`` imports this module, so importing it back at module load
    time would create a cycle).
    """
    from app.generation.rag.context_assembler import build_context_block
    from app.generation.rag.estimator import _generate

    if not kept_chunks:
        return {
            "estimate": estimate,
            "validation_passed": True,
            "confidence": _CONFIDENCE_SCORES.get(estimate.confidence),
        }

    context_block = build_context_block(kept_chunks)

    fabricated = validate_citations(estimate, kept_chunks)
    if fabricated:
        feedback = (
            f"your previous response cited invalid source ids: {fabricated}. "
            "Only cite ids that appear in the <sources> block."
        )
        estimate = await _generate(context_block, structured_query, feedback=feedback)
        if validate_citations(estimate, kept_chunks):
            estimate = estimate.model_copy(update={"confidence": "low"})

    if not check_coherence(estimate):
        feedback = (
            'when confidence is "insufficient", total_engineer_days and '
            "duration_weeks must be null, modules must be empty and "
            "insufficient_context_explanation must be filled; otherwise provide "
            "the modules, tasks and numbers."
        )
        estimate = await _generate(context_block, structured_query, feedback=feedback)
        if not check_coherence(estimate):
            return {
                "estimate": estimate,
                "validation_passed": False,
                "confidence": _CONFIDENCE_SCORES.get(estimate.confidence),
            }

    return {
        "estimate": estimate,
        "validation_passed": True,
        "confidence": _CONFIDENCE_SCORES.get(estimate.confidence),
    }
