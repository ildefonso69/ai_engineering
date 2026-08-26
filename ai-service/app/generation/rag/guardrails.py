"""Session 16 — the deterministic checks around the RAG estimate.

Two guardrails, at the two ends of the pipeline, both of them code that runs
whatever the model does:

* **Input** — the Session 4 checks (moderation, prompt injection, PII) finally
  applied to ``/v1/estimate/from-transcript``. Until now they guarded only
  ``/api/v1/estimate``, so the endpoint the whole project is built around — the
  one that takes a raw client transcript and feeds it to a reasoning model — had
  no input check at all. That was the real security hole, and it was invisible
  because nothing failed.

* **Output** — is the number defensible given the evidence? The arithmetic lives
  in ``foundation/guardrails/estimate_bounds.py``; this module adapts the RAG
  types to it and turns the verdict into review reasons.

Neither one rejects the estimate. A failed bound means "a person should look at
this", and the response says so. Throwing away work the client already paid for,
because a threshold someone picked was crossed, trades a quality problem for an
outage.
"""

from __future__ import annotations

from typing import Sequence

import structlog

from app.config import Settings, get_settings
from app.foundation.guardrails.estimate_bounds import BoundsVerdict, check_total_bounds
from app.generation.rag.schemas import Estimate, RetrievedChunk

log = structlog.get_logger()

__all__ = ["evidence_hours", "bounds_for", "review_reasons_for_estimate"]


def evidence_hours(chunks: Sequence[RetrievedChunk]) -> list[int]:
    """The historical hours behind an estimate, counted ONCE per chunk.

    Deduplication is not tidiness, it is what makes the guardrail work. A single
    retrieved component is typically cited by four or five task lines, so summing
    per citation inflates the evidence base several-fold — and a guardrail whose
    denominator grows with the model's verbosity can never fire. Counted once per
    chunk id, the base is what the retriever actually put in front of the model.
    """
    seen: dict[int, int] = {}
    for chunk in chunks:
        if chunk.estimated_hours and chunk.id not in seen:
            seen[chunk.id] = int(chunk.estimated_hours)
    return list(seen.values())


def bounds_for(
    estimate: Estimate,
    chunks: Sequence[RetrievedChunk],
    *,
    settings: Settings | None = None,
) -> BoundsVerdict:
    """Run the magnitude guardrail over a produced estimate."""
    settings = settings or get_settings()
    return check_total_bounds(
        estimate.total_engineer_days,
        evidence_hours(chunks),
        settings=settings,
    )


def review_reasons_for_estimate(
    estimate: Estimate,
    verdict: BoundsVerdict,
    *,
    settings: Settings | None = None,
) -> list[str]:
    """Why a person should look at this estimate. Empty list == ship it.

    A PURE function of the estimate and the verdict — no I/O, no clock. Same
    discipline as ``domain/graph/supervisor/gate.py::review_reasons``, and for a
    related reason: a rule that reads the wall clock gives different answers to
    the same estimate, which makes the audit trail worthless.

    Note that an ABSTENTION is not a review reason. The system saying "I do not
    have enough data" is the system working correctly, and routing every
    abstention to a human would train reviewers to rubber-stamp.
    """
    settings = settings or get_settings()
    reasons = list(verdict.reasons)

    if estimate.confidence == "low" and estimate.total_engineer_days is not None:
        reasons.append("the model reported low confidence but still returned a number")

    ungrounded = [
        task.name
        for module in estimate.modules
        for task in module.tasks
        if not task.grounded
    ]
    total_lines = sum(len(module.tasks) for module in estimate.modules)
    if total_lines and len(ungrounded) > total_lines / 2:
        reasons.append(
            f"{len(ungrounded)} of {total_lines} lines have no source backing them"
        )

    if reasons:
        log.info(
            "estimate_flagged_for_review",
            reasons=len(reasons),
            total_engineer_days=estimate.total_engineer_days,
            evidence_engineer_days=round(verdict.evidence_engineer_days, 1),
            ratio=None if verdict.ratio is None else round(verdict.ratio, 2),
        )
    return reasons
