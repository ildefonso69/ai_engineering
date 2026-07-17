"""``proposal_agent`` (BONUS) — drafts a commercial proposal from the estimate.

Runs only after the final human gate has VALIDATED the estimate (and the human asked
for a proposal). It writes a client-facing proposal grounded strictly in the validated
estimate — no new scope, no invented numbers. Gated by ``GRAPH_PROPOSAL_ENABLED`` and
the gate-2 ``want_proposal`` flag (see ``build.route_after_gate2``).
"""

from __future__ import annotations

import asyncio

import logfire
import structlog

from app.config import get_settings
from app.domain.graph.personas import persona_for
from app.domain.graph.schemas import CommercialProposal

log = structlog.get_logger()

_PROPOSAL_SYSTEM_PROMPT = (
    "You are a delivery lead writing a concise commercial proposal for a client, based "
    "STRICTLY on a validated software estimate (modules → tasks with engineer-days) and "
    "its reliability report. Write a title, a 2-4 sentence executive summary, a bullet "
    "scope of the modules/deliverables, echo the total engineer-days, and a full "
    "proposal body in Markdown. Do NOT invent scope, prices or numbers not present in "
    "the estimate. Keep it honest and client-ready."
)


def _proposal_input(estimate: dict, analysis_report: dict) -> str:
    lines = [
        f"total_engineer_days: {estimate.get('total_engineer_days')}",
        f"confidence: {estimate.get('confidence')}",
        f"reliability_summary: {(analysis_report or {}).get('summary', '')}",
        "modules:",
    ]
    for module in estimate.get("modules") or []:
        task_days = [
            t.get("estimated_hours") for t in (module.get("tasks") or []) if t.get("estimated_hours")
        ]
        lines.append(
            f"  - {module.get('name')}: {len(module.get('tasks') or [])} tasks, "
            f"{sum(task_days)}h total"
        )
    return "\n".join(lines)


async def build_proposal(
    estimate: dict, analysis_report: dict, *, persona: str | None = None
) -> CommercialProposal:
    """Draft a full ``CommercialProposal`` from a validated estimate.

    The reusable core of the proposal agent: pure over its ``estimate`` /
    ``analysis_report`` dict inputs, so it powers both the graph node AND the
    standalone ``POST …/graph/{id}/proposal`` endpoint (generate a proposal after the
    run completed, without re-running the graph). ``persona`` is prepended to the
    system prompt when the agent is played in character.
    """
    settings = get_settings()
    from app.dependencies import get_llm_wrapper

    wrapper = get_llm_wrapper()
    system_prompt = f"{persona}\n\n{_PROPOSAL_SYSTEM_PROMPT}" if persona else _PROPOSAL_SYSTEM_PROMPT
    user_message = _proposal_input(estimate or {}, analysis_report or {})
    proposal, _meta = await asyncio.to_thread(
        wrapper.complete_structured,
        system_prompt=system_prompt,
        user_message=user_message,
        response_model=CommercialProposal,
        model_override=settings.GRAPH_PROPOSAL_MODEL,
    )
    return proposal


async def proposal_agent(state: dict) -> dict:
    """Validated estimate → commercial proposal (Markdown). Graph node wrapper."""
    with logfire.span("node: proposal_agent"):
        persona = persona_for(
            "proposal_agent", enabled=get_settings().GRAPH_PERSONAS_ENABLED
        )
        proposal = await build_proposal(
            state.get("estimate") or {}, state.get("analysis_report") or {}, persona=persona
        )
        log.info("agent_proposal_done", title=proposal.title, scope=len(proposal.scope))
        return {"proposal": proposal.body_markdown}
