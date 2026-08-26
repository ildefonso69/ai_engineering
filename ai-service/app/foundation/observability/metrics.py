"""Per-request token/cost accumulation (Session 16).

Until Session 16 the service could tell you a request's *latency* but not what it
*cost*: token counts existed only inside individual LLM calls, and the structured
path threw them away entirely. This module is the missing accumulator.

The shape is deliberately small. One :class:`RequestMetrics` per HTTP request,
held in a :mod:`contextvars` variable so nothing has to be threaded through the
call stack — the same technique ``app/main.py`` already uses to bind
``request_id`` for structlog. Every LLM call adds to it; the middleware reads it
once at the end and emits a single ``request_completed`` event.

**Why a mutable object rather than an immutable counter.** Most of the pipeline
runs sync work through ``asyncio.to_thread``, and ``to_thread`` executes in a
*copy* of the context: rebinding a ContextVar inside the thread would be
invisible to the caller. Mutating the object the variable points at is visible,
because both contexts hold the same reference. Every ``+=`` below is in-place on
purpose.

Outside a request — CLI scripts, the eval harness, tests — there is no active
accumulator and :func:`record_llm_call` is a no-op. Instrumentation must never be
the reason something fails.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any

__all__ = [
    "RequestMetrics",
    "begin_request",
    "current_metrics",
    "end_request",
    "record_llm_call",
]


@dataclass
class RequestMetrics:
    """Running totals for one request.

    ``llm_calls`` counts round-trips, not retries-as-separate-requests: when
    Instructor re-prompts on a validation error it is the same logical call, and
    the wrapper records it once with the usage of the final attempt.
    """

    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, *, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
        self.llm_calls += 1
        self.prompt_tokens += max(0, int(tokens_in or 0))
        self.completion_tokens += max(0, int(tokens_out or 0))
        self.cost_usd += float(cost_usd or 0.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            # Six decimals: a gpt-4o-mini call can cost less than a thousandth
            # of a cent, and rounding it to 2 would report every cheap request
            # as free.
            "cost_usd": round(self.cost_usd, 6),
        }


_current: contextvars.ContextVar[RequestMetrics | None] = contextvars.ContextVar(
    "request_metrics", default=None
)


def begin_request() -> contextvars.Token:
    """Start a fresh accumulator. Returns the token :func:`end_request` needs."""
    return _current.set(RequestMetrics())


def end_request(token: contextvars.Token) -> RequestMetrics:
    """Close the accumulator opened by ``token`` and return its totals."""
    metrics = _current.get() or RequestMetrics()
    _current.reset(token)
    return metrics


def current_metrics() -> RequestMetrics | None:
    """The accumulator for the request in flight, or ``None`` outside one."""
    return _current.get()


def record_llm_call(*, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
    """Add one LLM call to the request in flight. No-op outside a request."""
    metrics = _current.get()
    if metrics is None:
        return
    metrics.add(tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd)
