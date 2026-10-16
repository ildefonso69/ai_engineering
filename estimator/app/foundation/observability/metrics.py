"""Production request metrics (Session 16 — golden set / observability close-out).

Emits the 3 signals the exercise's dashboard needs (latency, cost, error rate)
for every real estimation request:

- A structured ``structlog`` event, keeping the data visible in the normal
  log stream / Logfire console spans (Session 13's console-only decision,
  no token/account) alongside the per-stage ``log_stage`` events already
  emitted by the RAG pipeline.
- A row in the durable ``request_metrics`` Postgres table (see
  ``alembic/versions/0004_session16_request_metrics.py``), because container
  stdout is ephemeral and the dashboard needs something it can query
  reliably. Reuses the project's existing ``estimator-postgres`` instance —
  no new infra.

``foundation/`` is the right layer: both ``generation/rag/estimator.py`` and
``app/api/routers/estimate.py`` need to call this, and foundation has no
layering restriction against being used by callers above it.
"""

from __future__ import annotations

import structlog
from sqlalchemy import text

from app.foundation.llm.wrapper import _estimate_cost
from app.foundation.persistence.database import create_engine_from_settings

log = structlog.get_logger()


def estimate_cost(prompt_tokens: int, completion_tokens: int, model: str) -> float:
    """Approximate USD cost for one LLM call. Delegates to the LLM wrapper's
    ``_estimate_cost`` (same ``MODEL_COSTS`` pricing table) so the two never
    drift apart. Prices are illustrative and will go stale as providers
    reprice — fine for a teaching dashboard, not billing-grade."""
    return _estimate_cost(model, prompt_tokens, completion_tokens)


def record_request_metrics(
    *,
    request_id: str,
    endpoint: str,
    latency_ms: int,
    prompt_tokens: int,
    completion_tokens: int,
    status: str,
    model: str | None = None,
    estimated_cost_usd: float | None = None,
) -> None:
    """Log + persist one request's production metrics.

    ``status`` is a free-form label ("success", "guardrail_violation",
    "generation_failed", "http_502", ...) — the dashboard only needs to know
    success vs. not-success to compute an error rate, so no enum is enforced
    here. Persistence failures are logged and swallowed: a metrics-table
    outage must never take down the estimation request it is measuring.

    This function is synchronous and blocking on purpose (plain
    ``Engine.begin()``/``conn.execute``, no asyncpg stack here). Async
    callers (e.g. ``estimate_from_transcript``) MUST offload it via
    ``asyncio.to_thread`` — calling it bare from an ``async def`` would
    stall the whole event loop on Postgres I/O.
    """
    cost = estimated_cost_usd
    if cost is None and model is not None:
        cost = estimate_cost(prompt_tokens, completion_tokens, model)
    cost = cost or 0.0

    log.info(
        "estimate_request_metrics",
        request_id=request_id,
        endpoint=endpoint,
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_cost_usd=cost,
        status=status,
    )

    try:
        engine = create_engine_from_settings()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO request_metrics "
                    "(request_id, endpoint, latency_ms, prompt_tokens, completion_tokens, "
                    "estimated_cost_usd, status) "
                    "VALUES (:request_id, :endpoint, :latency_ms, :prompt_tokens, "
                    ":completion_tokens, :estimated_cost_usd, :status)"
                ),
                {
                    "request_id": request_id,
                    "endpoint": endpoint,
                    "latency_ms": latency_ms,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "estimated_cost_usd": cost,
                    "status": status,
                },
            )
    except Exception as exc:  # noqa: BLE001 — metrics must never break the request path
        log.error(
            "request_metrics_persist_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
