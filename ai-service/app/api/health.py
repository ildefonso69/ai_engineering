"""Liveness and readiness probes.

Two probes that answer two different questions, and the difference is the whole
point:

``GET /health``  (defined in ``app/main.py``) — **liveness**. "Is the process
    up?" It touches nothing: no database, no Redis, and above all no LLM. It is
    what Docker's ``HEALTHCHECK`` and compose's ``depends_on: service_healthy``
    call every 30 seconds, so anything it did would be billed 2 880 times a day.

``GET /health/ready`` (here) — **readiness**. "Can this process actually serve a
    request?" It checks the dependencies the service cannot work without — the
    vector database and Redis — and returns **503** when one of them is down, so
    an orchestrator can pull the instance out of rotation instead of routing
    traffic into a guaranteed failure.

The readiness probe still does **not** call the LLM. A model call costs money and
takes seconds; a probe that does it would turn a rate-limited provider into a
self-inflicted outage. Provider reachability is a concern for the estimate
endpoints, which surface it as a 503 of their own.

Both paths are exempt from the ``X-Service-Token`` middleware: a container
healthcheck and a platform probe cannot carry a secret. Neither reveals anything
beyond the names of the dependencies.
"""

from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.config import get_settings

log = structlog.get_logger()

router = APIRouter(tags=["health"])

# A probe must fail fast. If a dependency cannot answer in this many seconds it
# is unavailable *for practical purposes*, whatever it is doing internally.
PROBE_TIMEOUT_SECONDS = 2.0


async def _check_vector_db() -> tuple[bool, str]:
    """``SELECT 1`` against the vector database. Cheapest possible round trip
    that still proves the connection pool can hand out a working connection."""
    try:
        from app.foundation.persistence.database import get_async_session_factory

        factory = get_async_session_factory()
        async with factory() as session:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")), timeout=PROBE_TIMEOUT_SECONDS
            )
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 — any failure means "not ready"
        return False, type(exc).__name__


async def _check_redis() -> tuple[bool, str]:
    """``PING`` against Redis. Covers both CAG caches and the idempotency store."""
    client = None
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(
            get_settings().REDIS_URL,
            socket_connect_timeout=PROBE_TIMEOUT_SECONDS,
            socket_timeout=PROBE_TIMEOUT_SECONDS,
        )
        await asyncio.wait_for(client.ping(), timeout=PROBE_TIMEOUT_SECONDS)
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 — closing a dead client is not a failure
                pass


@router.get("/health/ready")
async def readiness(response: Response) -> dict:
    """Report whether every hard dependency is reachable.

    200 with ``status="ready"`` when all checks pass, 503 with
    ``status="not_ready"`` otherwise. The per-dependency breakdown is always
    included so the failing one is named without reading the logs.
    """
    vector_db_ok, vector_db_detail = await _check_vector_db()
    redis_ok, redis_detail = await _check_redis()

    checks = {
        "vector_db": {"ok": vector_db_ok, "detail": vector_db_detail},
        "redis": {"ok": redis_ok, "detail": redis_detail},
    }
    ready = vector_db_ok and redis_ok

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        log.warning(
            "readiness_failed",
            vector_db=vector_db_detail,
            redis=redis_detail,
        )

    return {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
    }
