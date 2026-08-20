"""Session 15 — the AI service's HTTP contract, pinned.

The two services deploy independently, so the contract has to be enforced by a
test rather than by everyone remembering it. Three things are pinned here:

1. **The status codes are part of the contract.** 401 (two independent auth
   layers), 422 (invalid input), 503 (a dependency is down). The business
   backend branches on each of them, so a silent change here becomes a
   confusing failure over there.
2. **The routes the business backend consumes exist**, verified against the same
   JSON artifact `scripts/check_contract.py` uses in CI.
3. **The probes stay open.** `/health` and `/health/ready` must never require the
   service token: a container healthcheck cannot present a credential.

Network-free and LLM-free.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import search as search_module
from app.api.service_token import EXEMPT_PATHS
from app.config import get_settings
from app.dependencies import get_semantic_retriever
from app.main import app

CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "contract"
    / "business-backend-consumed-routes.json"
)

TOKEN = "s15-contract-token"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def secured_client(monkeypatch):
    """A client for which the service token is switched ON."""
    monkeypatch.setattr(get_settings(), "AI_SERVICE_TOKEN", TOKEN)
    return TestClient(app)


# --- 1. status codes are the contract ------------------------------------- #


def test_401_when_the_service_token_is_missing(secured_client):
    r = secured_client.post("/api/v1/estimate", json={})

    assert r.status_code == 401
    assert r.json()["detail"]["reason"] == "invalid_service_token"
    assert r.headers["WWW-Authenticate"] == "X-Service-Token"


def test_401_when_the_api_key_is_missing_even_with_a_valid_token(secured_client):
    """The two layers are independent: the token says *whether* you may talk to
    the service, the key says *which* endpoints. A valid token must not open a
    key-protected router."""
    r = secured_client.post(
        "/v1/retrieval/search",
        json={"query_text": "a query long enough to pass validation"},
        headers={"X-Service-Token": TOKEN},
    )

    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "X-API-Key"


def test_422_on_invalid_input(client):
    """Pydantic rejects a description below the minimum length before any work
    -- and before any token spend.

    Deliberately probed on `/api/v1/estimate`, which carries no `X-API-Key`:
    on a key-protected router the 401 fires first (auth precedes validation),
    so that route could never show a clean 422.
    """
    r = client.post("/api/v1/estimate", json={"description": "too short"})

    assert r.status_code == 422


def test_503_when_a_dependency_is_unavailable(client):
    """Session 15: an absent embedder is an unavailable dependency, not a bug in
    the request. 503 tells the caller that retrying may work; 500 would not."""
    app.dependency_overrides[get_semantic_retriever] = lambda: None
    try:
        r = client.post("/search", json={"query": "anything", "k": 5})
    finally:
        app.dependency_overrides.pop(get_semantic_retriever, None)

    assert r.status_code == 503
    assert r.json()["detail"] == "Embedding service is not available."


def test_500_is_still_reserved_for_genuine_failures(client):
    """The 503 change must not swallow real bugs: an exploding dependency is
    still a 500. Distinguishing the two is the point of the contract."""

    class Exploding:
        async def search(self, **kwargs):
            raise RuntimeError("embeddings API down")

    app.dependency_overrides[get_semantic_retriever] = lambda: Exploding()
    try:
        r = client.post("/search", json={"query": "anything", "k": 5})
    finally:
        app.dependency_overrides.pop(get_semantic_retriever, None)

    assert r.status_code == 500
    assert search_module is not None  # module seam kept explicit for the reader


# --- 2. the consumed routes exist ----------------------------------------- #


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text())


def test_the_contract_artifact_is_present_and_non_empty():
    """If this file disappears, CI's contract job silently checks nothing."""
    assert CONTRACT_PATH.exists(), f"missing contract artifact: {CONTRACT_PATH}"
    assert len(_contract()["routes"]) >= 25


def test_every_route_the_business_backend_calls_exists():
    schema = app.openapi()["paths"]
    missing = [
        f"{r['method'].upper()} {r['path']} ({r['client']})"
        for r in _contract()["routes"]
        if r["path"] not in schema or r["method"].lower() not in schema[r["path"]]
    ]

    assert not missing, "routes consumed by the business backend but absent here: " + str(missing)


# --- 3. the probes stay open ---------------------------------------------- #


@pytest.mark.parametrize("path", ["/health", "/health/ready"])
def test_probes_never_require_the_service_token(secured_client, path):
    assert path in EXEMPT_PATHS

    r = secured_client.get(path)

    # 200 (ready) or 503 (a dependency is down) -- both mean the probe RAN.
    # 401 would mean the healthcheck can never pass and compose never starts.
    assert r.status_code != 401
