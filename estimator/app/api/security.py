"""API-key authentication for the Session 9 routers, plus the Session 15
service-to-service token for the Session 4 estimation endpoint.

Two independent keys protect the S09 routers: a ``RETRIEVAL_API_KEY`` holder
cannot call the estimate endpoint and vice versa. Keys are compared with
``secrets.compare_digest`` (constant-time) — never ``==``, which leaks length
and prefix information through timing.
"""

from __future__ import annotations

import secrets

import structlog
from fastapi import Header, HTTPException, status

from app.config import get_settings

log = structlog.get_logger()

_API_KEY_HEADER = "X-API-Key"


def _verify(provided: str | None, expected: str | None) -> None:
    """Raise 401 unless ``provided`` matches the configured ``expected`` key."""
    if not expected or not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": _API_KEY_HEADER},
        )


async def require_retrieval_key(
    x_api_key: str | None = Header(default=None, alias=_API_KEY_HEADER),
) -> None:
    """FastAPI dependency guarding ``POST /v1/retrieval/search``."""
    _verify(x_api_key, get_settings().RETRIEVAL_API_KEY)


async def require_estimate_key(
    x_api_key: str | None = Header(default=None, alias=_API_KEY_HEADER),
) -> None:
    """FastAPI dependency guarding ``POST /v1/estimate/from-transcript``."""
    _verify(x_api_key, get_settings().ESTIMATE_API_KEY)


_SERVICE_TOKEN_HEADER = "X-Service-Token"


async def require_service_token(
    x_service_token: str | None = Header(default=None, alias=_SERVICE_TOKEN_HEADER),
) -> None:
    """FastAPI dependency guarding ``POST /api/v1/estimate`` (S04, docker-compose isolation).

    Unlike ``require_retrieval_key``/``require_estimate_key`` (where an unset
    key means "endpoint disabled"), an unset ``AI_SERVICE_TOKEN`` leaves this
    endpoint OPEN: it is the long-standing S04 endpoint that every prior
    session's tests and live-demo curl commands call unauthenticated, so a
    "secure by default" flip would break all of that. The token is meant to
    be set specifically by the docker-compose orchestration (Session 15),
    where ``estimator`` is not reachable from the host at all and this header
    is the only thing standing between ``estimator-web`` and anyone else on
    the internal network.
    """
    expected = get_settings().AI_SERVICE_TOKEN
    if expected is None:
        return
    if not x_service_token or not secrets.compare_digest(x_service_token, expected):
        log.warning(
            "service_token_rejected",
            header_present=x_service_token is not None,
        )
    _verify(x_service_token, expected)
