"""Session 15 — liveness vs readiness.

The two probes must stay different in a specific way: liveness may never touch a
dependency (it runs every 30 s from the Docker HEALTHCHECK), and readiness must
report 503 when one is down (so a platform can pull the instance out of
rotation). Neither may ever call the LLM.

Network-free: both dependency checks are monkeypatched at the seam the router
imports them from.
"""

import pytest
from fastapi.testclient import TestClient

from app.api import health as health_module
from app.config import get_settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def _fake_checks(monkeypatch, *, vector_db=True, redis=True):
    async def fake_vector_db():
        return (vector_db, "ok" if vector_db else "OperationalError")

    async def fake_redis():
        return (redis, "ok" if redis else "ConnectionError")

    monkeypatch.setattr(health_module, "_check_vector_db", fake_vector_db)
    monkeypatch.setattr(health_module, "_check_redis", fake_redis)


def test_liveness_touches_nothing(client, monkeypatch):
    """``/health`` must answer even with every dependency dead — that is the
    whole point of separating it from readiness."""
    _fake_checks(monkeypatch, vector_db=False, redis=False)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_readiness_is_200_when_every_dependency_answers(client, monkeypatch):
    _fake_checks(monkeypatch)

    response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["vector_db"]["ok"] is True
    assert body["checks"]["redis"]["ok"] is True


@pytest.mark.parametrize(
    "down,expected_key",
    [("vector_db", "vector_db"), ("redis", "redis")],
)
def test_readiness_is_503_and_names_the_broken_dependency(
    client, monkeypatch, down, expected_key
):
    _fake_checks(monkeypatch, **{down: False})

    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"][expected_key]["ok"] is False
    # The breakdown must name the failure without anyone reading the logs.
    assert body["checks"][expected_key]["detail"] != "ok"


def test_readiness_needs_no_service_token(monkeypatch):
    """A platform probe cannot carry a secret, so readiness is exempt exactly
    like liveness. Without this the service would look permanently unhealthy."""
    from app.api.service_token import EXEMPT_PATHS

    assert "/health/ready" in EXEMPT_PATHS
    assert "/health" in EXEMPT_PATHS

    monkeypatch.setattr(get_settings(), "AI_SERVICE_TOKEN", "s15-secret-token")
    _fake_checks(monkeypatch)

    with TestClient(app) as c:
        assert c.get("/health/ready").status_code == 200
