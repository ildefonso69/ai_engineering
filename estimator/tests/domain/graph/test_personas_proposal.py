"""Matrix personas + the reusable ``build_proposal`` core (Session 13 live) — no network.

Verifies: ``persona_for`` honours the enabled flag / unknown nodes; and that
``build_proposal`` prepends the persona to the system prompt while staying a pure
function over its estimate/analysis dicts (LLM faked).
"""

from __future__ import annotations

from app.domain.graph.agents.proposal import _PROPOSAL_SYSTEM_PROMPT, build_proposal
from app.domain.graph.personas import NODE_PERSONAS, persona_for
from app.domain.graph.schemas import CommercialProposal


def test_persona_for_respects_enabled_and_unknown_nodes():
    assert persona_for("classifier_agent", enabled=False) is None
    assert persona_for("unknown_node", enabled=True) is None
    on = persona_for("analysis_agent", enabled=True)
    assert on is not None and on.startswith("You are the Oracle")
    # The guardrail line is always appended so the character can't break the output.
    assert "never sacrifice correctness" in on


def test_all_llm_nodes_have_a_persona():
    assert set(NODE_PERSONAS) == {
        "classifier_agent",
        "structure_agent",
        "recover_and_handover",
        "analysis_agent",
        "proposal_agent",
    }


class _CapturingWrapper:
    def __init__(self):
        self.system_prompt = None

    def complete_structured(self, *, system_prompt, user_message, response_model, **kwargs):
        self.system_prompt = system_prompt
        return (
            CommercialProposal(
                title="T",
                executive_summary="S",
                scope=["a"],
                total_engineer_days=10,
                body_markdown="# body",
            ),
            {"model": "fake"},
        )


async def test_build_proposal_prepends_persona(monkeypatch):
    wrapper = _CapturingWrapper()
    monkeypatch.setattr("app.dependencies.get_llm_wrapper", lambda: wrapper)
    estimate = {"total_engineer_days": 10, "confidence": "high", "modules": []}

    proposal = await build_proposal(estimate, {"summary": "ok"}, persona="You are the Architect.")
    assert isinstance(proposal, CommercialProposal)
    assert wrapper.system_prompt.startswith("You are the Architect.")
    assert _PROPOSAL_SYSTEM_PROMPT in wrapper.system_prompt


async def test_build_proposal_without_persona_uses_base_prompt(monkeypatch):
    wrapper = _CapturingWrapper()
    monkeypatch.setattr("app.dependencies.get_llm_wrapper", lambda: wrapper)
    await build_proposal({"modules": []}, {}, persona=None)
    assert wrapper.system_prompt == _PROPOSAL_SYSTEM_PROMPT
