"""Session 16 — publishing the production-signals dashboard over HTTP.

Network-free. What matters here is that the page is reachable by the business
backend, that a missing file degrades into something useful instead of a 404,
and — the one that is easy to get wrong — that it is NOT public.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import eval_reports
from app.api.service_token import SERVICE_TOKEN_HEADER
from app.config import get_settings
from app.main import app

client = TestClient(app)


def test_the_dashboard_is_served_as_html():
    response = client.get("/api/v1/eval/dashboard")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<html" in response.text


def test_the_page_is_self_contained():
    """It is embedded in an iframe by the business backend and must not phone out.

    No script tags, no external stylesheets: the sparkline is SVG that the
    generator emitted. If this ever regresses, the panel silently breaks for
    anyone whose network blocks the host it started reaching for.
    """
    body = client.get("/api/v1/eval/dashboard").text

    assert "<script" not in body.lower()
    assert "http://" not in body and "https://" not in body


def test_a_missing_dashboard_explains_how_to_produce_one(monkeypatch, tmp_path):
    """A 404 would be correct and useless. The reader needs the command."""
    monkeypatch.setattr(eval_reports, "DASHBOARD_HTML", tmp_path / "nope.html")
    response = client.get("/api/v1/eval/dashboard")

    assert response.status_code == 200
    assert "refresh_dashboard.sh" in response.text


def test_the_aggregates_are_available_as_data():
    payload = client.get("/api/v1/eval/dashboard.json").json()

    assert "overall" in payload
    assert "by_path" in payload


def test_missing_aggregates_come_back_empty_not_as_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(eval_reports, "DASHBOARD_JSON", tmp_path / "nope.json")
    payload = client.get("/api/v1/eval/dashboard.json").json()

    assert payload["generated_at"] is None
    assert payload["by_path"] == []


def test_the_dashboard_is_not_public(monkeypatch: pytest.MonkeyPatch):
    """Latency, cost and error rate per endpoint are an operational profile.

    The business backend carries the service token already, so there is nothing
    to gain from exempting this path — and something real to lose.
    """
    monkeypatch.setattr(get_settings(), "AI_SERVICE_TOKEN", "s16-secret")
    secured = TestClient(app)

    assert secured.get("/api/v1/eval/dashboard").status_code == 401
    assert (
        secured.get(
            "/api/v1/eval/dashboard", headers={SERVICE_TOKEN_HEADER: "s16-secret"}
        ).status_code
        == 200
    )
