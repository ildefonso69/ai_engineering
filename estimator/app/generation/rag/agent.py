"""Agentic estimation orchestrator (Session 12).

A manual agent loop using OpenAI's Responses API with custom function calling.
The agent receives a transcript, decomposes it into components, searches for
historical budgets per component, and consolidates into a final estimate.

The loop is conducted by hand (not delegated to the API's internal agentic
behavior) to capture reasoning and decisions at each step.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import structlog
from openai import AsyncOpenAI

from app.generation.rag.agent_tools import calculate_estimate, search_budgets
from app.generation.rag.schemas import Estimate, EstimationQuery, SourceCitation, WorkModule, TaskItem

log = structlog.get_logger()


@dataclass
class AgentStep:
    """One iteration of the agent loop."""

    step_num: int
    reasoning: str
    action_type: str  # "search_budgets" or "calculate_estimate"
    action_detail: str
    observation: str
    tool_result: dict[str, Any] = field(default_factory=dict)


class Agent:
    """Manual agent orchestrator for multi-component estimation."""

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str = "gpt-5-mini",
        max_iterations: int = 5,
        reasoning_effort: str = "medium",
    ):
        """Initialize the agent.

        Parameters
        ----------
        client : AsyncOpenAI
            Authenticated OpenAI client.
        model : str
            Model to use (default: gpt-5-mini for debugging, gpt-5 for production).
        max_iterations : int
            Maximum loop iterations (safeguard against infinite loops).
        reasoning_effort : str
            Reasoning effort level ("low", "medium", "high").
        """
        self.client = client
        self.model = model
        self.max_iterations = max_iterations
        self.reasoning_effort = reasoning_effort
        self.trace: list[AgentStep] = []

    def _build_system_prompt(self) -> str:
        """Build the system prompt that defines the agent's role and method."""
        return """You are an estimation agent. Your task is to:

1. Analyze the provided meeting transcript.
2. Identify the components or requirements to estimate (e.g., backend, frontend, integrations).
3. Use the search_budgets tool to find historical budget references for EACH component separately.
4. After collecting references for all components, use calculate_estimate to aggregate them.
5. Provide a final consolidated estimate.

Method:
- Do NOT try to estimate everything in one search. Break down the project into distinct components.
- For each component, call search_budgets with a focused query (e.g., "backend business logic", "mobile app").
- Collect results from multiple searches; note patterns in hours/effort across components.
- Once you have enough references for all identified components, call calculate_estimate with the component data.
- Provide a summary of your findings and the final estimate.

You have two tools available:
- search_budgets: retrieve historical budgets for a specific component or requirement
- calculate_estimate: aggregate components and their reference amounts into a total estimate

Be systematic and thorough. Make multiple searches if needed, but set a reasonable limit (max 4-5 searches).
After consolidation, provide your final answer."""

    def _build_tools_schema(self) -> list[dict[str, Any]]:
        """Build the JSON schema for the Responses API tools."""
        return [
            {
                "type": "function",
                "name": "search_budgets",
                "description": (
                    "Search historical budgets and cost references for a specific component, "
                    "requirement, or functional area. Use this tool to build up references for "
                    "different project components (e.g., backend, mobile, integrations, DevOps). "
                    "Call multiple times for different components. Returns historical data that "
                    "informs your estimate."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Component or requirement description. "
                                "Examples: 'backend business logic', 'REST API integration', "
                                "'mobile app development', 'database schema design'."
                            ),
                        },
                        "filters": {
                            "type": "object",
                            "description": (
                                "Optional filters (sector, date_range, component_type, etc.). "
                                "Use when you need to narrow the search."
                            ),
                            "properties": {
                                "sector": {"type": "string"},
                                "date_range": {"type": "string"},
                                "component_type": {"type": "string"},
                            },
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "type": "function",
                "name": "calculate_estimate",
                "description": (
                    "Aggregate components and their historical reference amounts into a "
                    "consolidated total estimate. Call this AFTER you have collected references "
                    "for all major components via search_budgets. Provide the component names "
                    "and the reference amounts (hours or engineer-days) from your searches."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "components": {
                            "type": "array",
                            "description": "List of components with their reference amounts.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {
                                        "type": "string",
                                        "description": "Component name (e.g., 'Backend', 'Mobile App').",
                                    },
                                    "reference_amounts": {
                                        "type": "array",
                                        "items": {"type": "number"},
                                        "description": "Historical reference amounts (hours/days) for this component.",
                                    },
                                },
                                "required": ["name", "reference_amounts"],
                            },
                        },
                    },
                    "required": ["components"],
                },
            },
        ]

    async def _execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool and return its result."""
        if tool_name == "search_budgets":
            return await search_budgets(
                query=tool_input.get("query", ""),
                filters=tool_input.get("filters"),
            )
        elif tool_name == "calculate_estimate":
            return calculate_estimate(components=tool_input.get("components", []))
        else:
            return {"error": f"Unknown tool: {tool_name}"}

    async def run(self, transcript: str) -> tuple[Estimate, list[AgentStep]]:
        """Run the agent loop.

        Parameters
        ----------
        transcript : str
            The meeting transcript.

        Returns
        -------
        tuple[Estimate, list[AgentStep]]
            Final estimate and trace of steps.
        """
        system_prompt = self._build_system_prompt()
        tools_schema = self._build_tools_schema()

        messages = [{"role": "user", "content": transcript}]
        final_response = None
        step_num = 0

        for iteration in range(self.max_iterations):
            log.info("agent_iteration", iteration=iteration, step_count=step_num)

            # Call the API with function calling and reasoning
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system_prompt,
                tools=tools_schema,
                tool_choice="auto",
                messages=messages,
            )

            # Extract reasoning from content blocks (if reasoning effort was applied)
            reasoning_text = ""
            tool_calls = []

            for block in response.content:
                # Check for text content (final response)
                if hasattr(block, "text"):
                    reasoning_text = block.text
                # Check for tool use
                elif hasattr(block, "type") and block.type == "tool_use":
                    tool_calls.append(block)

            # If no tool calls, this is the final response
            if not tool_calls:
                final_response = response
                break

            # Process each function call
            tool_results = []
            for call in tool_calls:
                step_num += 1
                tool_name = call.name
                # Arguments from the LLM may be a dict or JSON string
                tool_input = call.input if isinstance(call.input, dict) else json.loads(str(call.input))

                # Execute the tool
                result = await self._execute_tool(tool_name, tool_input)

                # Record step in trace
                input_str = json.dumps(tool_input)
                action_detail = (
                    f"{tool_name}({input_str[:80]}...)"
                    if len(input_str) > 80
                    else f"{tool_name}({input_str})"
                )
                result_str = json.dumps(result)
                observation = (
                    result_str[:180] + "..."
                    if len(result_str) > 180
                    else result_str
                )

                step = AgentStep(
                    step_num=step_num,
                    reasoning=reasoning_text[:180] + "..." if len(reasoning_text) > 180 else reasoning_text,
                    action_type=tool_name,
                    action_detail=action_detail,
                    observation=observation,
                    tool_result=result,
                )
                self.trace.append(step)

                log.info(
                    "agent_step",
                    step=step_num,
                    tool=tool_name,
                    observation_count=result.get("count", 0),
                )

                # Build tool result for the API
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": json.dumps(result),
                    }
                )

            # Add the assistant response and tool results to messages for next iteration
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

        # Extract the final estimate from the last response
        # For now, create a placeholder estimate
        final_estimate = Estimate(
            total_engineer_days=None,
            modules=[],
            duration_weeks=None,
            sources=[],
            assumptions=[],
            confidence="medium",
            reasoning="Estimated via agentic loop.",
        )

        log.info("agent_completed", steps=len(self.trace))
        return final_estimate, self.trace

    def format_trace(self) -> str:
        """Format the trace as a readable string."""
        lines = ["=== Agent Trace ==="]
        for step in self.trace:
            lines.append(f"\nSTEP {step.step_num}")
            lines.append(f"  Reasoning: {step.reasoning}")
            lines.append(f"  Action: {step.action_detail}")
            lines.append(f"  Observation: {step.observation}")
        lines.append("\n=== End Trace ===")
        return "\n".join(lines)
