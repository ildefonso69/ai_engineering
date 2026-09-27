"""``POST /v1/estimate/from-transcript`` — transcript → grounded estimate (S09).

The endpoint the whole project has been building toward: a raw client meeting
transcript in, a citation-backed engineer-day estimate out. It is the strictest
endpoint on the service — a tight rate limit (10/min) and idempotency, because
each call can drive a multi-step LLM pipeline.

Thin transport: validation in ``EstimateRequest`` (422), auth in
``require_estimate_key`` (401), rate limiting in the decorator (429). Any
pipeline failure becomes a 502 (the request id is on the ``X-Request-ID``
response header for debugging).
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.rate_limiting import limiter
from app.api.security import require_estimate_key
from app.dependencies import get_estimation_service
from app.generation.agentic.supervisor_graph import (
    GraphNotFoundError,
    GraphNotPausedError,
    ToolPrivilegeViolation,
)
from app.generation.rag.errors import RagError
from app.generation.rag.estimator import estimate_from_transcript, estimate_from_transcript_agentic
from app.generation.rag.schemas import (
    AwaitingHumanReviewResponse,
    Estimate,
    EstimateRequest,
    ResumeEstimateRequest,
)

log = structlog.get_logger()

router = APIRouter(prefix="/v1/estimate", tags=["estimate"])


@router.post(
    "/from-transcript",
    response_model=Estimate,
    dependencies=[Depends(require_estimate_key)],
)
@limiter.limit("10/minute")
async def from_transcript(request: Request, payload: EstimateRequest) -> Estimate:
    """Produce a grounded estimate from a raw transcript (idempotent on key)."""
    try:
        return await estimate_from_transcript(
            payload.transcript,
            idempotency_key=payload.idempotency_key,
        )
    except RagError as exc:
        log.error("estimate_failed", error_type=type(exc).__name__, error=str(exc)[:300])
        raise HTTPException(status_code=502, detail="Failed to produce an estimate.") from exc


@router.post(
    "/from-transcript-agentic",
    response_model=Estimate,
    dependencies=[Depends(require_estimate_key)],
)
@limiter.limit("10/minute")
async def from_transcript_agentic(request: Request, payload: EstimateRequest) -> Estimate:
    """Produce an estimate using the agentic multi-component pipeline (Session 12).

    Uses a manual agent loop to handle multi-component projects by identifying
    components, searching separately for each, and consolidating results.
    """
    try:
        return await estimate_from_transcript_agentic(
            payload.transcript,
            idempotency_key=payload.idempotency_key,
        )
    except RagError as exc:
        log.error("estimate_agentic_failed", error_type=type(exc).__name__, error=str(exc)[:300])
        raise HTTPException(status_code=502, detail="Agentic estimation failed.") from exc


@router.post(
    "/from-transcript-graph",
    response_model=Estimate,
    dependencies=[Depends(require_estimate_key)],
)
@limiter.limit("10/minute")
async def from_transcript_graph(request: Request, payload: EstimateRequest) -> Estimate:
    """Produce an estimate via the LangGraph re-expression of the pipeline (Session 13).

    Sequential 5-node graph (extract_requirements -> classify_components ->
    search_budgets -> generate_estimate -> validate_and_consolidate), checkpointed
    on the project's Postgres with the idempotency key as thread_id. Same
    ``Estimate`` contract as the other two estimation endpoints.
    """
    try:
        service = get_estimation_service()
        return await service.estimate_from_transcript_graph(
            payload.transcript,
            idempotency_key=payload.idempotency_key,
        )
    except RagError as exc:
        log.error("estimate_graph_failed", error_type=type(exc).__name__, error=str(exc)[:300])
        raise HTTPException(status_code=502, detail="Graph-based estimation failed.") from exc


@router.post(
    "/from-transcript-multiagent",
    response_model=Estimate | AwaitingHumanReviewResponse,
    dependencies=[Depends(require_estimate_key)],
)
@limiter.limit("10/minute")
async def from_transcript_multiagent(
    request: Request, payload: EstimateRequest
) -> Estimate | AwaitingHumanReviewResponse:
    """Produce an estimate via the supervisor/workers multi-agent topology (Session 14).

    A hand-built supervisor node (``StateGraph`` + ``langgraph.types.Command``)
    routes to four specialist agents -- ``requirements_extractor``,
    ``budget_searcher``, ``estimate_generator``, ``coherence_validator`` --
    each restricted to exactly one tool, followed by a ``human_review_gate``
    that pauses the graph (``interrupt()``) when confidence is low, the
    estimate falls outside the historical range, or the transcript has no
    precedent in the budget corpus. When it pauses, this returns
    ``AwaitingHumanReviewResponse`` instead of an ``Estimate`` -- resume via
    ``POST /v1/estimate/{estimation_id}/resume``. Checkpointed on the
    project's Postgres with the idempotency key (or a generated uuid) as
    ``thread_id`` / ``estimation_id``.
    """
    try:
        service = get_estimation_service()
        outcome = await service.estimate_from_transcript_multiagent(
            payload.transcript,
            idempotency_key=payload.idempotency_key,
        )
        if outcome.status == "awaiting_human_review":
            return AwaitingHumanReviewResponse(
                estimation_id=outcome.estimation_id, reasons=outcome.reasons
            )
        return outcome.estimate
    except ToolPrivilegeViolation as exc:
        log.error(
            "estimate_multiagent_tool_violation", error_type=type(exc).__name__, error=str(exc)[:300]
        )
        raise HTTPException(status_code=502, detail="Multi-agent estimation failed.") from exc
    except RagError as exc:
        log.error("estimate_multiagent_failed", error_type=type(exc).__name__, error=str(exc)[:300])
        raise HTTPException(status_code=502, detail="Multi-agent estimation failed.") from exc


@router.post(
    "/{estimation_id}/resume",
    response_model=Estimate,
    dependencies=[Depends(require_estimate_key)],
)
@limiter.limit("10/minute")
async def resume_multiagent_estimation(
    request: Request, estimation_id: str, payload: ResumeEstimateRequest
) -> Estimate:
    """Resume a multiagent run paused by ``human_review_gate``.

    ``estimation_id`` is the ``thread_id`` returned by
    ``AwaitingHumanReviewResponse``. Folds the human's ``decision`` (approve /
    adjust / reject) into state via ``Command(resume=...)`` and runs the
    graph to completion.

    Verified behavior (not just the previous, untested claim): a
    ``thread_id`` for which no checkpoint exists at all returns 404; a
    ``thread_id`` whose checkpoint exists but has no pending interrupt --
    because it already ran to completion, including a previous call to this
    same endpoint, or because ``human_review_gate`` never paused it -- returns
    409. Neither case is a silent no-op replay. See
    ``supervisor_graph.resume_supervisor_graph`` for the ``aget_state`` check
    that enforces this before ``Command(resume=...)`` is ever issued.
    """
    try:
        service = get_estimation_service()
        outcome = await service.resume_multiagent_estimation(
            estimation_id, payload.decision.model_dump(mode="json")
        )
        if outcome.status == "awaiting_human_review":
            # Resuming a decision never re-triggers the same gate (the
            # interrupt() call it went through is now resolved), but a
            # defensive 502 beats silently handing back a half-finished state.
            raise RagError("Resume did not reach a finished estimate.")
        return outcome.estimate
    except GraphNotFoundError as exc:
        log.error("estimate_resume_thread_not_found", error_type=type(exc).__name__, error=str(exc)[:300])
        raise HTTPException(
            status_code=404, detail="No paused estimation found for this id."
        ) from exc
    except GraphNotPausedError as exc:
        log.error("estimate_resume_not_paused", error_type=type(exc).__name__, error=str(exc)[:300])
        raise HTTPException(
            status_code=409, detail="This estimation is not awaiting human review."
        ) from exc
    except ToolPrivilegeViolation as exc:
        log.error("estimate_resume_tool_violation", error_type=type(exc).__name__, error=str(exc)[:300])
        raise HTTPException(status_code=502, detail="Failed to resume the estimation.") from exc
    except RagError as exc:
        log.error("estimate_resume_failed", error_type=type(exc).__name__, error=str(exc)[:300])
        raise HTTPException(status_code=502, detail="Failed to resume the estimation.") from exc
