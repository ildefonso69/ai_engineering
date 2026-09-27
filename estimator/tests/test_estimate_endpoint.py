"""End-to-end tests for POST /api/v1/estimate (structured output).

The pipeline is mocked at the ``EstimationService`` level: the test swaps the
real service for a fake whose ``estimate`` records the request it received and
returns a canned ``EstimationResponse``. This isolates the endpoint from
network access while still exercising request validation and response shaping.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.api.security as security
from app.config import Settings
from app.dependencies import get_estimation_service
from app.main import app
from app.domain.schemas.estimation import EstimationRequest, EstimationResponse, EstimationResult


def _canned_result() -> EstimationResult:
    return EstimationResult(
        summary="Mid-sized B2B SaaS for equipment loans across teams.",
        total_duration_weeks=8,
        total_cost_eur=30_000,
        confidence_pct=70,
        phases=[
            {"name": "Discovery", "duration_weeks": 1, "cost_eur": 5_000,
             "summary": "Workshops, scoping and tech spike."},
            {"name": "Implementation", "duration_weeks": 6, "cost_eur": 20_000,
             "summary": "Build the core SaaS features."},
            {"name": "QA + launch", "duration_weeks": 1, "cost_eur": 5_000,
             "summary": "Test pass and production rollout."},
        ],
    )


class FakeEstimationService:
    """Records the request and returns a canned response."""

    def __init__(self) -> None:
        self.calls: list[EstimationRequest] = []

    def estimate(self, request: EstimationRequest) -> EstimationResponse:
        self.calls.append(request)
        return EstimationResponse(
            result=_canned_result(), prompt_version="v1", cached=False
        )


@pytest.fixture
def fake_service() -> FakeEstimationService:
    svc = FakeEstimationService()
    app.dependency_overrides[get_estimation_service] = lambda: svc
    yield svc
    app.dependency_overrides.pop(get_estimation_service, None)


SERVICE_TOKEN = "shared-secret"


@pytest.fixture(autouse=True)
def _pin_service_token_settings(monkeypatch):
    """Pin AI_SERVICE_TOKEN to "unset" for every test in this file by
    default (reliability fix: the suite must not depend on whatever
    AI_SERVICE_TOKEN happens to be exported in the developer's/CI shell).
    The dedicated auth tests below override this per-test via the same
    ``monkeypatch`` fixture instance.
    """
    monkeypatch.setattr(
        security, "get_settings", lambda: type("S", (), {"AI_SERVICE_TOKEN": None})()
    )


VALID_PAYLOAD = {
    "description": "A small B2B SaaS to manage employee equipment loans across teams.",
    "project_type": "web_saas",
    "detail_level": "medium",
    "output_format": "phases_table",
}


def test_valid_payload_returns_structured_response(
    client: TestClient, fake_service: FakeEstimationService
) -> None:
    response = client.post("/api/v1/estimate", json=VALID_PAYLOAD)
    assert response.status_code == 200
    body = response.json()
    assert body["prompt_version"] == "v1"
    assert body["cached"] is False
    assert body["result"]["total_cost_eur"] == 30_000
    assert body["result"]["confidence_pct"] == 70
    assert len(body["result"]["phases"]) == 3
    assert body["result"]["phases"][0]["name"] == "Discovery"


def test_endpoint_forwards_request_to_service(
    client: TestClient, fake_service: FakeEstimationService
) -> None:
    client.post("/api/v1/estimate", json=VALID_PAYLOAD)
    assert len(fake_service.calls) == 1
    received = fake_service.calls[0]
    assert received.description == VALID_PAYLOAD["description"]
    assert received.project_type.value == "web_saas"


def test_missing_project_type_returns_422(client: TestClient, fake_service) -> None:
    payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "project_type"}
    response = client.post("/api/v1/estimate", json=payload)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(err["loc"][-1] == "project_type" for err in detail)


def test_invalid_enum_value_returns_422(client: TestClient, fake_service) -> None:
    payload = {**VALID_PAYLOAD, "project_type": "not_a_real_enum"}
    response = client.post("/api/v1/estimate", json=payload)
    assert response.status_code == 422


def test_short_description_returns_422(client: TestClient, fake_service) -> None:
    payload = {**VALID_PAYLOAD, "description": "too short"}
    response = client.post("/api/v1/estimate", json=payload)
    assert response.status_code == 422


# ---- Session 15 service-token auth contract (see app/api/security.py) ----


def test_service_token_unset_leaves_endpoint_open(
    client: TestClient, fake_service: FakeEstimationService
) -> None:
    """Named regression test for the documented default: no AI_SERVICE_TOKEN
    configured means the endpoint stays reachable with no header at all.
    Uses the autouse pin above (AI_SERVICE_TOKEN=None) explicitly, so a
    future change that flips this default fails loudly here."""
    response = client.post("/api/v1/estimate", json=VALID_PAYLOAD)
    assert response.status_code == 200


def test_service_token_set_with_correct_header_returns_200(
    client: TestClient, fake_service: FakeEstimationService, monkeypatch
) -> None:
    monkeypatch.setattr(
        security, "get_settings", lambda: type("S", (), {"AI_SERVICE_TOKEN": SERVICE_TOKEN})()
    )
    response = client.post(
        "/api/v1/estimate", json=VALID_PAYLOAD, headers={"X-Service-Token": SERVICE_TOKEN}
    )
    assert response.status_code == 200


def test_service_token_set_with_missing_header_returns_401(
    client: TestClient, fake_service: FakeEstimationService, monkeypatch
) -> None:
    monkeypatch.setattr(
        security, "get_settings", lambda: type("S", (), {"AI_SERVICE_TOKEN": SERVICE_TOKEN})()
    )
    response = client.post("/api/v1/estimate", json=VALID_PAYLOAD)
    assert response.status_code == 401


def test_service_token_set_with_wrong_header_returns_401(
    client: TestClient, fake_service: FakeEstimationService, monkeypatch
) -> None:
    monkeypatch.setattr(
        security, "get_settings", lambda: type("S", (), {"AI_SERVICE_TOKEN": SERVICE_TOKEN})()
    )
    response = client.post(
        "/api/v1/estimate", json=VALID_PAYLOAD, headers={"X-Service-Token": "wrong-token"}
    )
    assert response.status_code == 401


def test_empty_string_service_token_from_env_behaves_as_unset(
    client: TestClient, fake_service: FakeEstimationService, monkeypatch
) -> None:
    """The actual regression test for the bug three fresh-context reviewers
    found: Compose's ``environment: AI_SERVICE_TOKEN: ${AI_SERVICE_TOKEN}``
    sets the container env var to "" (not absent) whenever the root .env
    doesn't define it. This must build a real ``Settings()`` from that env
    var (not just call the dependency function in isolation), so it
    exercises the actual env -> pydantic-settings pipeline where the bug
    lived, and confirms "" behaves identically to a genuinely unset var.
    """
    monkeypatch.setenv("AI_SERVICE_TOKEN", "")
    # Settings requires at least one LLM API key; pin one so the real
    # Settings() construction below doesn't depend on ambient env either.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")

    real_settings = Settings()
    assert real_settings.AI_SERVICE_TOKEN is None

    monkeypatch.setattr(security, "get_settings", lambda: real_settings)
    response = client.post("/api/v1/estimate", json=VALID_PAYLOAD)
    assert response.status_code == 200
