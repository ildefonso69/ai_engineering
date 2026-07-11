"""The typed state's accumulator reducers (Level 1).

``budget_matches`` and ``errors`` are annotated with ``operator.add`` so LangGraph
CONCATENATES partial updates instead of overwriting. We assert that both on the
annotation metadata and on the compiled channel behaviour (no LLM needed).
"""

from __future__ import annotations

import operator
import typing

from langgraph.channels import BinaryOperatorAggregate
from langgraph.graph import END, START, StateGraph

from app.domain.graph.state import EstimationState


def _channels():
    """Channel spec of a minimal compiled graph over the state."""
    builder = StateGraph(EstimationState)
    builder.add_node("noop", lambda state: {})
    builder.add_edge(START, "noop")
    builder.add_edge("noop", END)
    return builder.compile().channels


def test_accumulator_fields_are_annotated_with_operator_add():
    hints = typing.get_type_hints(EstimationState, include_extras=True)
    assert operator.add in getattr(hints["budget_matches"], "__metadata__", ())
    assert operator.add in getattr(hints["errors"], "__metadata__", ())
    assert getattr(hints["requirements"], "__metadata__", ()) == ()


def test_accumulator_fields_compile_to_a_reducer_channel():
    channels = _channels()
    assert isinstance(channels["budget_matches"], BinaryOperatorAggregate)
    assert isinstance(channels["errors"], BinaryOperatorAggregate)
    assert not isinstance(channels["requirements"], BinaryOperatorAggregate)


def test_reducer_concatenates_partial_updates():
    channel = _channels()["budget_matches"].copy()
    channel.update(
        [[{"component": "A", "reference_budget_id": "b1", "amount": 80.0, "distance": 0.1}]]
    )
    channel.update(
        [[{"component": "B", "reference_budget_id": "b2", "amount": 40.0, "distance": 0.2}]]
    )
    assert [m["component"] for m in channel.get()] == ["A", "B"]


def test_errors_reducer_appends_without_clobbering():
    channel = _channels()["errors"].copy()
    channel.update([["first issue"]])
    channel.update([["second issue"]])
    assert channel.get() == ["first issue", "second issue"]
