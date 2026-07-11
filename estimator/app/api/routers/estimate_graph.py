"""``POST /v1/estimate/graph`` — the estimation flow as a LangGraph run (S13).

Same external contract as the other estimate endpoints (transcript in, structured
estimate + ``status`` out); underneath, it drives the compiled ``StateGraph`` built
at startup (``app.state.graph``) with the request's ``estimation_id`` as the
checkpointer ``thread_id``, so the run is persisted and resumable.

Thin transport, same posture as ``estimate_agent.py`` / ``estimate.py``: validation
in the request schema (422), auth in ``require_estimate_key`` (401, reuses
``ESTIMATE_API_KEY``), rate limiting in the decorator (429), graph/LLM failures →
502. When the graph could not be built at startup (e.g. the checkpointer's Postgres
was unreachable) ``app.state.graph`` is ``None`` and the endpoint returns 503.
"""

from __future__ import annotations

from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import get_request_id
from app.api.rate_limiting import limiter
from app.api.security import require_estimate_key
from app.domain.schemas.graph_estimation import GraphEstimateRequest, GraphEstimateResponse
from app.generation.rag.observability import log_stage

log = structlog.get_logger()

router = APIRouter(prefix="/v1/estimate", tags=["estimate-graph"])


@router.post(
    "/graph",
    response_model=GraphEstimateResponse,
    dependencies=[Depends(require_estimate_key)],
)
@limiter.limit("10/minute")
async def estimate_graph(request: Request, payload: GraphEstimateRequest) -> GraphEstimateResponse:
    """Run the estimation graph over a transcript and return estimate + status."""
    request_id = get_request_id(request)
    graph = getattr(request.app.state, "graph", None)
    if graph is None:
        log.error("graph_unavailable", request_id=request_id)
        raise HTTPException(status_code=503, detail="Estimation graph is not available.")

    estimation_id = payload.estimation_id or str(uuid4())
    config = {"configurable": {"thread_id": estimation_id}}
    try:
        with log_stage("graph_estimate", request_id, estimation_id=estimation_id):
            result = await graph.ainvoke(
                {"transcript": payload.transcript, "estimation_id": estimation_id},
                config,
            )
    except Exception as exc:  # noqa: BLE001 — any node/LLM failure → 502.
        log.error(
            "graph_estimate_failed",
            request_id=request_id,
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        raise HTTPException(status_code=502, detail="Failed to produce an estimate.") from exc

    return GraphEstimateResponse(
        estimation_id=estimation_id,
        status=result.get("status") or "needs_review",
        estimate=result.get("estimate"),
        requirements=result.get("requirements") or [],
        components=result.get("components") or [],
        budget_matches=result.get("budget_matches") or [],
        errors=result.get("errors") or [],
    )
