"""Per-stage structured logging for the RAG pipeline (Session 9).

``log_stage`` wraps each pipeline step so every stage emits a consistent
``stage.started`` / ``stage.completed`` (with ``duration_ms``) / ``stage.failed``
trio, all correlated by a shared ``request_id``. This makes a single request's
journey through reformulation → retrieval → augmentation → generation trivially
greppable in the JSON logs.

**Session 16 — cost attribution per stage.** ``stage.completed`` and
``stage.failed`` now also carry the tokens and dollars spent *inside* that stage,
read as a delta from the per-request accumulator in
``app/foundation/observability/metrics.py``. No new plumbing: the stages were
already wrapped, and the accumulator was already counting, so the two only had to
be introduced to each other.

Why per stage and not just per request. "This estimate cost $0.31" tells you
nothing you can act on. "Generation was $0.29 of it and reformulation $0.01" tells
you exactly where to look, and it is the number that decides whether a cheaper
model for the small steps is worth anything at all — usually it is not, and the
data says so before you spend a week on it.

A stage that runs no LLM call reports zeros. That is a fact worth having: it is how
you learn that retrieval, the stage everyone assumes is expensive, costs nothing
per request once the embedding is cached.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator

import structlog

from app.foundation.observability.metrics import current_metrics

log = structlog.get_logger()


def _spent() -> tuple[int, int, float]:
    """(prompt tokens, completion tokens, cost) so far in this request.

    Zeros outside a request — CLI runs and tests share these code paths, and
    instrumentation must never be the reason something fails.
    """
    metrics = current_metrics()
    if metrics is None:
        return 0, 0, 0.0
    return metrics.prompt_tokens, metrics.completion_tokens, metrics.cost_usd


@contextmanager
def log_stage(stage: str, request_id: str, **context: Any) -> Iterator[None]:
    """Log the lifecycle of one pipeline ``stage`` bound to ``request_id``.

    Parameters
    ----------
    stage:
        Stage name, e.g. ``"reformulation"`` or ``"generation"``.
    request_id:
        UUID correlating every stage of the same request.
    **context:
        Extra structured fields attached to all three events.

    Yields
    ------
    None
        The body runs inside the ``try``; any exception is logged as
        ``stage.failed`` (with ``duration_ms`` and the error type) and re-raised.
    """
    log.info("stage.started", stage=stage, request_id=request_id, **context)
    t0 = time.perf_counter()
    tokens_in_before, tokens_out_before, cost_before = _spent()

    def _delta() -> dict[str, Any]:
        tokens_in, tokens_out, cost = _spent()
        return {
            "stage_prompt_tokens": tokens_in - tokens_in_before,
            "stage_completion_tokens": tokens_out - tokens_out_before,
            "stage_cost_usd": round(cost - cost_before, 6),
        }

    try:
        yield
    except Exception as exc:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        log.error(
            "stage.failed",
            stage=stage,
            request_id=request_id,
            duration_ms=duration_ms,
            # A failed stage still spent money — often MORE than a successful one,
            # because it timed out after retrying. Omitting the cost here would
            # hide the most expensive requests the system makes.
            **_delta(),
            error_type=type(exc).__name__,
            error=str(exc)[:300],
            **context,
        )
        raise
    duration_ms = int((time.perf_counter() - t0) * 1000)
    log.info(
        "stage.completed",
        stage=stage,
        request_id=request_id,
        duration_ms=duration_ms,
        **_delta(),
        **context,
    )
