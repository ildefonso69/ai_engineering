"""Cross-cutting observability primitives for the AI service (Session 16).

``metrics`` accumulates per-request token and cost counters so the service can
emit ONE structured event per HTTP request instead of forcing a dashboard to
stitch together the individual LLM-call log lines.
"""

from app.foundation.observability.metrics import (
    RequestMetrics,
    begin_request,
    current_metrics,
    end_request,
    record_llm_call,
)

__all__ = [
    "RequestMetrics",
    "begin_request",
    "current_metrics",
    "end_request",
    "record_llm_call",
]
