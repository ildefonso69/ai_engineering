"""Unit tests for the manual agent loop, driven by a fake AsyncOpenAI client.

No network and no API key: a scripted fake client returns canned Responses API
outputs so we can assert the loop's control flow — multiple tool calls, call_id
echoing, the max-iterations safeguard and the trace shape.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.generation.agentic.agent_loop import run_estimation_agent
from app.generation.agentic.agent_schemas import (
    AgentComponent,
    AgentEstimate,
    SearchBudgetsArgs,
)


def _function_call(name: str, call_id: str, arguments: dict):
    return SimpleNamespace(
        type="function_call", name=name, call_id=call_id, arguments=json.dumps(arguments)
    )


def _reasoning(text: str):
    return SimpleNamespace(type="reasoning", summary=[SimpleNamespace(text=text)])


def _message():
    return SimpleNamespace(type="message", role="assistant", content=[])


class _FakeResponses:
    """Scripted ``responses.create`` / ``responses.parse`` double."""

    def __init__(self, scripted_outputs: list[list], parsed: AgentEstimate | None):
        self._scripted = scripted_outputs
        self._parsed = parsed
        self._i = 0
        self.create_calls: list[dict] = []
        self.parse_calls: list[dict] = []

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)
        output = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return SimpleNamespace(output=output, id=f"resp_{self._i}")

    async def parse(self, **kwargs):
        self.parse_calls.append(kwargs)
        return SimpleNamespace(output_parsed=self._parsed)


class _FakeClient:
    def __init__(self, responses: _FakeResponses):
        self.responses = responses


async def _stub_backend(args: SearchBudgetsArgs) -> list[dict]:
    return [{"id": 1, "estimated_hours": 100.0, "content_preview": "x", "distance": 0.1}]


def _happy_path_script():
    """Two searches → calculate → validate → final message (loop ends)."""
    return [
        [
            _reasoning("Decompose the project and search each component."),
            _function_call("search_budgets", "call_1", {"query": "auth backend", "filters": None}),
            _function_call("search_budgets", "call_2", {"query": "mobile app", "filters": None}),
        ],
        [
            _function_call(
                "calculate_estimate",
                "call_3",
                {"components": [{"name": "Auth", "reference_amounts": [100.0]}]},
            )
        ],
        [
            _function_call(
                "validate_estimate",
                "call_4",
                {
                    "components": [
                        {"name": "Auth", "estimated_hours": 115.0, "reference_amounts": [100.0]}
                    ],
                    "total_hours": 115.0,
                },
            )
        ],
        [_message()],
    ]


def _final_estimate() -> AgentEstimate:
    return AgentEstimate(
        components=[AgentComponent(name="Auth", estimated_hours=115.0, rationale="median+buffer")],
        total_hours=115.0,
        assumptions=["Rails/Postgres as stated."],
        confidence="medium",
    )


async def test_happy_path_multi_tool_run():
    fake = _FakeResponses(_happy_path_script(), _final_estimate())
    result = await run_estimation_agent(
        "transcript text",
        client=_FakeClient(fake),
        model="gpt-5-mini",
        max_iterations=10,
        retrieval_backend=_stub_backend,
    )

    tools_used = [step.tool for step in result.trace.steps]
    assert tools_used.count("search_budgets") == 2  # >1 search, per the acceptance criteria
    assert "calculate_estimate" in tools_used
    assert "validate_estimate" in tools_used

    # Every step carries reasoning + action + observation.
    for step in result.trace.steps:
        assert step.tool
        assert step.observation
    assert result.trace.steps[0].reasoning_summary is not None

    assert result.stopped_reason == "completed"
    assert result.estimate is not None
    assert result.estimate.total_hours == 115.0
    assert fake.parse_calls, "final structured parse should have been called"


async def test_call_ids_are_echoed_back():
    fake = _FakeResponses(_happy_path_script(), _final_estimate())
    await run_estimation_agent(
        "t",
        client=_FakeClient(fake),
        model="gpt-5-mini",
        retrieval_backend=_stub_backend,
    )
    # The 2nd create call carries the outputs for the first turn's two calls.
    second_call_input = fake.create_calls[1]["input"]
    echoed = {item["call_id"] for item in second_call_input}
    assert echoed == {"call_1", "call_2"}
    for item in second_call_input:
        assert item["type"] == "function_call_output"
        assert isinstance(item["output"], str)  # output must be a JSON string


async def test_max_iterations_safeguard_stops_loop():
    # A script that never stops calling a tool.
    never_stops = [[_function_call("search_budgets", "call_x", {"query": "loop", "filters": None})]]
    fake = _FakeResponses(never_stops, _final_estimate())
    result = await run_estimation_agent(
        "t",
        client=_FakeClient(fake),
        model="gpt-5-mini",
        max_iterations=3,
        retrieval_backend=_stub_backend,
    )
    assert result.stopped_reason == "max_iterations"
    assert result.estimate is None
    assert not fake.parse_calls  # no final parse when the safeguard trips
    assert result.iterations == 3


async def test_bad_tool_arguments_do_not_crash_the_loop():
    # First turn calls calculate_estimate with structurally invalid args, then stops.
    script = [
        [_function_call("calculate_estimate", "call_1", {"components": [{"name": "A"}]})],
        [_message()],
    ]
    fake = _FakeResponses(script, _final_estimate())
    result = await run_estimation_agent(
        "t",
        client=_FakeClient(fake),
        model="gpt-5-mini",
        retrieval_backend=_stub_backend,
    )
    # The bad call becomes an error observation, not an exception.
    assert result.trace.steps[0].tool == "calculate_estimate"
    assert "error" in result.trace.steps[0].observation.lower()
