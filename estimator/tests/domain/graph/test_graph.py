"""End-to-end graph run, network-free (Levels 1 + 3).

The compiled graph is driven with a ``MemorySaver`` checkpointer, a fake
``LLMWrapper`` (scripted structured outputs) and a fake retrieval backend — no
network, no API key, no database. We assert: the five nodes run in order, the
``budget_matches`` accumulator grows one entry per retrieved item, the estimate and
``status`` are set, and the Level-3 conditional edge routes to ``needs_review`` when
validation fails.
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.domain.graph.build import build_graph, route_on_status
from app.domain.graph.schemas import (
    ComponentClassification,
    ComponentEstimate,
    ComponentModel,
    ConsolidatedEstimate,
    RequirementsExtraction,
)

TRANSCRIPT = "A" * 200  # only its presence matters; the LLM node is faked.


class _FakeWrapper:
    """Scripted ``complete_structured`` double keyed on the response model."""

    def __init__(self, *, requirements, components, estimate):
        self._requirements = requirements
        self._components = components
        self._estimate = estimate
        self.calls: list[str] = []

    def complete_structured(self, *, system_prompt, user_message, response_model, **kwargs):
        self.calls.append(response_model.__name__)
        meta = {"model": "fake", "provider": "fake", "latency_ms": 1}
        if response_model is RequirementsExtraction:
            return RequirementsExtraction(requirements=self._requirements), meta
        if response_model is ComponentClassification:
            return ComponentClassification(components=self._components), meta
        if response_model is ConsolidatedEstimate:
            return self._estimate, meta
        raise AssertionError(f"unexpected response_model {response_model!r}")


def _fake_backend_factory(hours_by_component):
    """Build a ``make_retrieval_backend`` replacement returning canned items."""

    def _make(*_args, **_kwargs):
        async def _backend(query, sectors):
            # The query is "<name> (<category>)"; match on the component name prefix.
            name = query.split(" (")[0]
            hours = hours_by_component.get(name, [])
            return [
                {
                    "id": i,
                    "content_preview": f"historical {name}",
                    "sector": "logistics",
                    "budget_id": f"BUD-{name[:3].upper()}-{i}",
                    "estimated_hours": float(h),
                    "distance": 0.1 + 0.05 * i,
                }
                for i, h in enumerate(hours)
            ]

        return _backend

    return _make


def _wire(monkeypatch, *, wrapper, backend_factory):
    monkeypatch.setattr("app.dependencies.get_llm_wrapper", lambda: wrapper)
    monkeypatch.setattr("app.domain.graph.nodes.make_retrieval_backend", backend_factory)


async def _run(graph):
    return await graph.ainvoke(
        {"transcript": TRANSCRIPT, "estimation_id": "t1"},
        {"configurable": {"thread_id": "t1"}},
    )


@pytest.mark.asyncio
async def test_graph_runs_end_to_end_and_accumulates_budget_matches(monkeypatch):
    wrapper = _FakeWrapper(
        requirements=["business backend API", "SAP integration"],
        components=[
            ComponentModel(name="Business backend", category="backend"),
            ComponentModel(name="SAP integration", category="integration"),
        ],
        estimate=ConsolidatedEstimate(
            components=[
                ComponentEstimate(name="Business backend", engineer_days=12, rationale="anchored"),
                ComponentEstimate(name="SAP integration", engineer_days=20, rationale="anchored"),
            ],
            total_engineer_days=32,
            confidence="medium",
            reasoning="sum of grounded components",
        ),
    )
    # Two analogs for the first component, one for the second → 3 matches total.
    backend = _fake_backend_factory({"Business backend": [80, 120], "SAP integration": [160]})
    _wire(monkeypatch, wrapper=wrapper, backend_factory=backend)

    graph = build_graph(MemorySaver())
    state = await _run(graph)

    # Five nodes ran in order (three LLM calls in the right sequence).
    assert wrapper.calls == [
        "RequirementsExtraction",
        "ComponentClassification",
        "ConsolidatedEstimate",
    ]
    assert state["requirements"] == ["business backend API", "SAP integration"]
    assert [c["name"] for c in state["components"]] == ["Business backend", "SAP integration"]
    # Accumulator grew one BudgetMatch per retrieved analog.
    assert len(state["budget_matches"]) == 3
    assert {m["component"] for m in state["budget_matches"]} == {
        "Business backend",
        "SAP integration",
    }
    # Estimate + status set; total matches the component sum → validated.
    assert state["estimate"]["total_engineer_days"] == 32
    assert state["status"] == "validated"


@pytest.mark.asyncio
async def test_validation_failure_routes_to_needs_review(monkeypatch):
    wrapper = _FakeWrapper(
        requirements=["business backend API"],
        components=[ComponentModel(name="Business backend", category="backend")],
        # Total (999) does not match the component sum (12) → guardrail fails.
        estimate=ConsolidatedEstimate(
            components=[
                ComponentEstimate(name="Business backend", engineer_days=12, rationale="x"),
            ],
            total_engineer_days=999,
            confidence="low",
            reasoning="deliberately inconsistent",
        ),
    )
    backend = _fake_backend_factory({"Business backend": [80]})
    _wire(monkeypatch, wrapper=wrapper, backend_factory=backend)

    graph = build_graph(MemorySaver())
    state = await _run(graph)

    assert state["status"] == "needs_review"
    assert state["errors"]  # at least one guardrail issue recorded


def test_route_on_status_maps_status_to_branch():
    assert route_on_status({"status": "validated"}) == "validated"
    assert route_on_status({"status": "needs_review"}) == "needs_review"
    assert route_on_status({}) == "validated"  # default when unset
