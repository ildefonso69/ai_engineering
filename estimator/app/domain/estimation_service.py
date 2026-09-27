"""Pipeline orchestrator. Glue between guardrails, caches, prompt rendering and
the LLM wrapper. The router holds none of this logic — its only job is to
translate HTTP errors.

Pipeline (Session 4, final):

    1. Input guardrails (moderation + prompt injection + PII heuristics)
    2. Exact-match cache lookup  → return cached=True on hit
    3. Semantic cache lookup     → return cached=True on hit
    4. Render the versioned prompt
    5. LLM call via Instructor with response_model=EstimationResult
    6. Output guardrail (enforce_scope_response, filter policy)
    7. Write to BOTH caches (exact + semantic)
    8. Return EstimationResponse with cached=False

Order rationale: guardrails go before any cache because a malicious or PII
description should never be served from cache. The exact-match cache goes
before the semantic cache because it's the cheapest (no embedding call). The
semantic cache write happens AFTER output validation so we never cache failed
estimations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    # Type-only: keeps this module's runtime imports of generation/rag lazy
    # (same convention as the rest of this file -- the conductor is the one
    # place allowed to know about multiple generation siblings, but it still
    # avoids a module-level cross-sibling import at runtime).
    from app.generation.agentic.supervisor_graph import SupervisorGraphDependencies
    from app.generation.rag.schemas import Estimate

from app.config import get_settings
from app.generation.cag.semantic import EstimationSemanticCache
from app.foundation.guardrails.input import check_input
from app.foundation.guardrails.output import enforce_scope_response
from app.foundation.prompts import render_estimation_prompt
from app.foundation.prompts.loader import render_conversational_prompt
from app.domain.schemas.critic import CriticFeedback
from app.domain.schemas.estimation import (
    ACBResponse,
    DetailLevel,
    EstimationRequest,
    EstimationResponse,
    EstimationResult,
    OutputFormat,
    ProjectType,
    TurnObservation,
)
from app.generation.agentic.boss import Boss
from app.generation.cag.exact import EstimationCache
from app.generation.agentic.critic import Critic
from app.foundation.llm.wrapper import LLMWrapper
from app.generation.conversation.compression import apply_compression
from app.generation.conversation.metadata_extractor import update_metadata
from app.generation.conversation.models import Session
from app.generation.conversation.tier_resolver import Tier, resolve_tier

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Session 13 — LangGraph re-expression (graph nodes call back into these
# closures). Kept as small, private, module-level Pydantic models rather than
# new entries in generation/rag/schemas.py to keep this session's footprint
# additive-only; they describe an LLM output shape internal to the graph, not
# a new public contract.
# ---------------------------------------------------------------------------


class _Component(BaseModel):
    name: str = Field(description="Short component name, e.g. 'Auth', 'Payments'.")
    category: str = Field(description="Coarse functional category, e.g. 'backend', 'infra'.")
    requirements: list[str] = Field(
        default_factory=list, description="Subset of the extracted requirements this covers."
    )


class _ComponentBreakdown(BaseModel):
    components: list[_Component] = Field(default_factory=list)


@dataclass(frozen=True)
class MultiagentEstimationOutcome:
    """Session 14: what the multiagent conductor hands the router.

    ``estimate`` is ``None`` exactly when ``status == "awaiting_human_review"``
    -- the router branches on ``status`` alone and never has to inspect raw
    LangGraph state (no ``__interrupt__`` key, no ``SupervisorState``) itself.
    """

    estimate: Estimate | None
    status: str
    estimation_id: str
    reasons: list[str] = field(default_factory=list)  # only when awaiting_human_review


def _exact_cache_key(request: EstimationRequest, prompt_version: str, model: str) -> str:
    """Deterministic SHA-256 key over the typed request + prompt_version + model."""
    payload = json.dumps(
        {
            "description": request.description,
            "project_type": request.project_type.value,
            "detail_level": request.detail_level.value,
            "output_format": request.output_format.value,
            "prompt_version": prompt_version,
            "model": model,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"estimation:v2:{digest}"


class EstimationService:
    """Single entry point for the structured estimation pipeline."""

    def __init__(
        self,
        *,
        llm_wrapper: LLMWrapper,
        exact_cache: EstimationCache,
        semantic_cache: EstimationSemanticCache | None = None,
        openai_client: Any | None = None,
        prompt_version: str = "v1",
        conversational_prompt_version: str = "v3",
        metadata_extractor_model: str = "gpt-4o-mini",
        compression_model: str = "gpt-4o-mini",
        anchor_detection_mode: str = "heuristic",
        critic_model: str = "gpt-4o-mini",
        boss_max_iterations: int = 2,
        runtime_config: Any | None = None,
    ) -> None:
        self.llm_wrapper = llm_wrapper
        self.exact_cache = exact_cache
        self.semantic_cache = semantic_cache
        self.openai_client = openai_client
        self.prompt_version = prompt_version
        self.conversational_prompt_version = conversational_prompt_version
        # Constructor values are the fallbacks; with a runtime config wired
        # (Settings UI), the auxiliary models resolve per call instead.
        self.metadata_extractor_model = metadata_extractor_model
        self.compression_model = compression_model
        self.anchor_detection_mode = anchor_detection_mode
        self.critic_model = critic_model
        self.boss_max_iterations = boss_max_iterations
        self._runtime_config = runtime_config

    # ------------------------------------------------------------------
    # Effective auxiliary models (runtime override > constructor default)
    # ------------------------------------------------------------------

    def _effective_model(self, key: str, fallback: str) -> str:
        if self._runtime_config is not None:
            return self._runtime_config.effective(key)
        return fallback

    def _metadata_model(self) -> str:
        return self._effective_model("METADATA_EXTRACTOR_MODEL", self.metadata_extractor_model)

    def _compression_model(self) -> str:
        return self._effective_model("COMPRESSION_MODEL", self.compression_model)

    def _critic_model(self) -> str:
        return self._effective_model("CRITIC_MODEL", self.critic_model)

    def estimate(self, request: EstimationRequest) -> EstimationResponse:
        # 1. Input guardrails — raises InputGuardrailViolation on rejection.
        check_input(request.description, openai_client=self.openai_client)

        # 2. Exact-match cache lookup.
        cache_key = _exact_cache_key(request, self.prompt_version, self.llm_wrapper.primary_model)
        cached = self.exact_cache.get(cache_key)
        if cached:
            log.info("estimation_cache_hit", kind="exact", key_prefix=cache_key[:24])
            result = EstimationResult.model_validate(cached["result"])
            return EstimationResponse(
                result=result, prompt_version=self.prompt_version, cached=True
            )

        # 3. Semantic cache lookup (bucketed by model: a hit from another
        # model must never be served — the primary can change at runtime).
        if self.semantic_cache is not None:
            semantic_hit = self.semantic_cache.lookup(
                request, self.prompt_version, self.llm_wrapper.primary_model
            )
            if semantic_hit is not None:
                log.info("estimation_cache_hit", kind="semantic")
                return EstimationResponse(
                    result=semantic_hit,
                    prompt_version=self.prompt_version,
                    cached=True,
                )

        # 4. Render the versioned prompt.
        system_prompt, user_message = render_estimation_prompt(request, version=self.prompt_version)

        # 5. LLM call with Instructor + Pydantic validators (re-prompts on failure).
        result, meta = self.llm_wrapper.complete_structured(
            system_prompt=system_prompt,
            user_message=user_message,
            response_model=EstimationResult,
        )
        log.info(
            "estimation_generated",
            prompt_version=self.prompt_version,
            confidence_pct=result.confidence_pct,
            total_cost_eur=result.total_cost_eur,
            phases=len(result.phases),
            **meta,
        )

        # 6. Output guardrail (filter): normalises low-confidence answers.
        result = enforce_scope_response(result)

        # 7. Cache the validated payload only (never persist failed validations).
        self.exact_cache.set(
            cache_key,
            {
                "result": result.model_dump(mode="json"),
                "prompt_version": self.prompt_version,
            },
        )
        if self.semantic_cache is not None:
            self.semantic_cache.store(
                request, result, self.prompt_version, self.llm_wrapper.primary_model
            )

        # 8. Return.
        return EstimationResponse(result=result, prompt_version=self.prompt_version, cached=False)

    def estimate_conversational(
        self,
        *,
        session: Session,
        transcript: str,
        project_type: ProjectType,
        detail_level: DetailLevel,
        output_format: OutputFormat,
        tier: Tier | None = None,
        attachments_total_chars: int = 0,
    ) -> EstimationResponse:
        """Multi-turn estimation pipeline (Session 5).

        Differences with ``estimate``:
        - No exact/semantic caching: every turn depends on the conversation
          history + metadata, so two identical transcripts in different
          sessions are NOT the same call. ``cached`` is always ``False``.
        - The system prompt is the v2 template, which embeds the current
          ``ProjectMetadata`` block. The LLM also receives the prior
          user/assistant turns.
        - After validation, the session's history is appended and a second
          LLM call refreshes ``ProjectMetadata``.
        """
        # 1. Input guardrail on the enriched transcript (the caller has
        #    already concatenated any extracted attachment text into it).
        check_input(transcript, openai_client=self.openai_client)

        # 2. Resolve the audience tier. Override (when the caller passed one)
        #    wins; otherwise the rule chain decides from transcript + metadata.
        resolved_tier, rule = resolve_tier(
            transcript=transcript,
            metadata=session.metadata,
            override=tier,
        )
        session.last_resolved_tier = resolved_tier.value
        session.last_tier_rule = rule

        # 3. Render the conversational system + user prompts. v3 carries the
        #    <audience> block driven by the resolved tier; v2 ignores it.
        system_prompt, user_message = render_conversational_prompt(
            description=transcript,
            project_type=project_type,
            detail_level=detail_level,
            output_format=output_format,
            metadata=session.metadata,
            version=self.conversational_prompt_version,
            tier=resolved_tier,
        )

        # 3. Build the messages array: fresh system + prior history (already
        #    bounded by the sliding window) + current user.
        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        messages.extend(session.history.to_messages())
        messages.append({"role": "user", "content": user_message})

        log.info(
            "estimation_conversational_request",
            session_id=session.session_id,
            history_messages=len(session.history.messages),
            metadata_is_empty=session.metadata.is_empty(),
            transcript_chars=len(transcript),
        )

        # 4. LLM call with Instructor + Pydantic validators.
        result, meta = self.llm_wrapper.complete_structured_chat(
            messages=messages,
            response_model=EstimationResult,
        )
        log.info(
            "estimation_conversational_generated",
            session_id=session.session_id,
            confidence_pct=result.confidence_pct,
            total_cost_eur=result.total_cost_eur,
            phases=len(result.phases),
            **meta,
        )

        # 5. Output guardrail (filter policy: normalises low-confidence answers).
        result = enforce_scope_response(result)

        # 6. Append the turn to the history. The append is a pure data
        #    operation now — compression (anchor promotion + cumulative
        #    summary + sliding window) is the next, explicit step.
        session.history.append(user=user_message, assistant=result.model_dump_json())
        # Capture turn_index BEFORE compression: post-compression the sliding
        # window plateaus at ``max_turns`` and ``len(messages) // 2`` would
        # stop reflecting how many turns the session has actually seen.
        turn_index = len(session.history.messages) // 2
        apply_compression(
            session.history,
            llm_wrapper=self.llm_wrapper,
            compression_model=self._compression_model(),
            anchor_detection_mode=self.anchor_detection_mode,
        )

        # 7. Second-pass extractor refreshes ProjectMetadata. Failure is
        #    swallowed inside update_metadata (returns previous unchanged).
        session.metadata = update_metadata(
            previous=session.metadata,
            transcript=transcript,
            result=result,
            llm_wrapper=self.llm_wrapper,
            model=self._metadata_model(),
        )

        # 8. Emit the unified per-turn observation. Single structured event
        #    (rather than five log lines) makes the stress runner trivial: it
        #    reads ``response.observation`` straight from the JSON and never
        #    has to reconcile timestamps. ``cache_hit_kind`` is "none"
        #    because the conversational path bypasses both caches by design.
        observation = TurnObservation(
            turn_index=max(1, turn_index),
            session_id=session.session_id,
            enriched_transcript_chars=len(transcript),
            attachments_total_chars=attachments_total_chars,
            messages_in_window=len(session.history.messages),
            anchors_count=len(session.history.anchors),
            summary_chars=len(session.history.summary or ""),
            tokens_in=int(meta.get("tokens_in", 0) or 0),
            tokens_out=int(meta.get("tokens_out", 0) or 0),
            cost_usd=float(meta.get("cost_usd", 0.0) or 0.0),
            latency_ms=int(meta.get("latency_ms", 0) or 0),
            cache_hit_kind="none",
            last_resolved_tier=session.last_resolved_tier,
        )
        log.info("turn_observed", **observation.model_dump())

        return EstimationResponse(
            result=result,
            prompt_version=self.conversational_prompt_version,
            cached=False,
            observation=observation,
        )

    def estimate_with_acb(
        self,
        *,
        session: Session,
        transcript: str,
        project_type: ProjectType,
        detail_level: DetailLevel,
        output_format: OutputFormat,
        tier: Tier | None = None,
    ) -> ACBResponse:
        """Actor-Critic-Boss variant of the conversational pipeline.

        The session is updated **only** with the final Boss-approved (or
        Boss-synthesized) result — intermediate actor drafts are throwaway.
        That keeps the conversation state coherent: from the user's point of
        view, the turn produced exactly one assistant message.
        """

        # 1. Input guardrail.
        check_input(transcript, openai_client=self.openai_client)

        # 2. Resolve tier (same as actor path).
        resolved_tier, rule = resolve_tier(
            transcript=transcript,
            metadata=session.metadata,
            override=tier,
        )
        session.last_resolved_tier = resolved_tier.value
        session.last_tier_rule = rule

        log.info(
            "estimation_acb_request",
            session_id=session.session_id,
            tier=resolved_tier.value,
            tier_rule=rule,
            transcript_chars=len(transcript),
        )

        # 3. Build the actor callable. It re-renders the prompt each
        #    iteration so that critic feedback (if any) is woven in. The
        #    output guardrail runs on every actor draft; if a draft is
        #    eventually accepted by the Boss, that same guardrailed result
        #    is the one persisted to the session below.
        def _actor(critic_feedback: CriticFeedback | None) -> EstimationResult:
            system_prompt, user_message = render_conversational_prompt(
                description=transcript,
                project_type=project_type,
                detail_level=detail_level,
                output_format=output_format,
                metadata=session.metadata,
                version=self.conversational_prompt_version,
                tier=resolved_tier,
                critic_feedback=critic_feedback,
            )
            messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
            messages.extend(session.history.to_messages())
            messages.append({"role": "user", "content": user_message})

            draft, meta = self.llm_wrapper.complete_structured_chat(
                messages=messages,
                response_model=EstimationResult,
            )
            log.info(
                "acb_actor_draft",
                with_critic_feedback=critic_feedback is not None,
                issues_in_feedback=(
                    len(critic_feedback.issues) if critic_feedback is not None else 0
                ),
                confidence_pct=draft.confidence_pct,
                total_cost_eur=draft.total_cost_eur,
                **meta,
            )
            return enforce_scope_response(draft)

        # 4. Build the critic callable.
        critic = Critic(llm_wrapper=self.llm_wrapper, model=self._critic_model())

        def _critic(draft: EstimationResult) -> CriticFeedback:
            return critic.review(
                transcript=transcript,
                metadata=session.metadata,
                tier=resolved_tier,
                result=draft,
            )

        # 5. Boss orchestrates.
        boss = Boss(max_iterations=self.boss_max_iterations)
        final_result, trace = boss.run(actor=_actor, critic=_critic)

        # 6. Persist the final result into the session (single turn append).
        session.history.append(user=transcript, assistant=final_result.model_dump_json())
        apply_compression(
            session.history,
            llm_wrapper=self.llm_wrapper,
            compression_model=self._compression_model(),
            anchor_detection_mode=self.anchor_detection_mode,
        )

        # 7. Refresh metadata from the final result.
        session.metadata = update_metadata(
            previous=session.metadata,
            transcript=transcript,
            result=final_result,
            llm_wrapper=self.llm_wrapper,
            model=self._metadata_model(),
        )

        return ACBResponse(
            result=final_result,
            prompt_version=self.conversational_prompt_version,
            cached=False,
            acb=trace,
        )

    async def estimate_from_transcript_graph(
        self,
        transcript: str,
        idempotency_key: str | None = None,
    ):
        """LangGraph re-expression of ``estimate_from_transcript`` (Session 13).

        ``generation/agentic/graph.py`` cannot import ``generation/rag`` (sibling
        rule in ARCHITECTURE.md), so this method is where the two compose: it
        wires plain-dict callables (:class:`GraphDependencies`) over the S9-S12
        pure functions, runs the graph, then validates the final state's raw
        estimate dict back into a typed :class:`Estimate` -- same return type
        as ``estimate_from_transcript``/``estimate_from_transcript_agentic``,
        nothing about the public contract changes.
        """
        from app.dependencies import get_llm_wrapper, get_token_encoder
        from app.generation.agentic.graph import GraphDependencies, run_estimation_graph
        from app.generation.rag.agent_tools import search_budgets as rag_search_budgets
        from app.generation.rag.context_assembler import (
            build_context_block,
            truncate_to_token_budget,
        )
        from app.generation.rag.estimator import _generate, generate_estimate as rag_generate
        from app.generation.rag.query_reformulator import reformulate_query
        from app.generation.rag.schemas import Estimate, EstimationQuery, RetrievedChunk
        from app.generation.rag.validation import check_coherence, validate_citations

        settings = get_settings()

        def _insufficient(explanation: str) -> dict[str, Any]:
            return Estimate(
                total_engineer_days=None,
                duration_weeks=None,
                confidence="insufficient",
                reasoning="Retrieval did not surface enough relevant historical budgets.",
                insufficient_context_explanation=explanation,
            ).model_dump(mode="json")

        async def _extract_requirements(transcript: str) -> tuple[list[str], dict[str, Any]]:
            # Node 1 reuses the S9 reformulation stage: an EstimationQuery is
            # already "the requirements that drive retrieval", just structured
            # differently -- flatten it into a discrete requirements list.
            query = await reformulate_query(transcript)
            requirements = [query.function] if query.function else []
            requirements += [f"Use {tech}" for tech in query.technologies]
            requirements += query.constraints
            requirements += [f"Comply with {reg}" for reg in query.regulations]
            return requirements, query.model_dump(mode="json")

        async def _classify_components(
            requirements: list[str], structured_query: dict[str, Any]
        ) -> list[dict[str, Any]]:
            # Node 2 has no S9-S12 equivalent (the S12 agent's decomposition was
            # a raw, unused function-calling loop -- not reused here); a small
            # structured call over LLMWrapper groups requirements into
            # components, mirroring every other structured-output call in the
            # codebase.
            wrapper = get_llm_wrapper()
            breakdown, _meta = wrapper.complete_structured(
                system_prompt=(
                    "Group the given software requirements into functional "
                    "components (e.g. Auth, Payments, Data, Frontend, Infra). "
                    "Every requirement must belong to exactly one component."
                ),
                user_message=json.dumps(
                    {"requirements": requirements, "brief": structured_query}
                ),
                response_model=_ComponentBreakdown,
                model_override=settings.REFORMULATION_MODEL,
            )
            return [c.model_dump(mode="json") for c in breakdown.components]

        async def _search_component_budgets(component: dict[str, Any]) -> dict[str, Any]:
            # Node 3, per component: reuses the S12 retrieval tool as-is.
            query_text = component.get("name", "") + ": " + "; ".join(
                component.get("requirements", [])
            )
            result = await rag_search_budgets(query_text)
            return {
                "component": component.get("name"),
                "chunks": result.get("items", []),
                "low_confidence": bool(result.get("low_confidence", not result.get("items"))),
            }

        async def _generate_estimate(
            structured_query: dict[str, Any], search_results: list[dict[str, Any]]
        ) -> dict[str, Any]:
            # Node 4: assemble every retrieved chunk into one context block and
            # run the same grounded-generation call as the linear pipeline.
            raw_chunks = [chunk for entry in search_results for chunk in entry.get("chunks", [])]
            chunks = [
                RetrievedChunk(
                    id=c["chunk_id"],
                    content=c["content"],
                    sector=c["sector"],
                    project_year=c["project_year"],
                    chunk_type="historical_task",
                    distance=c["distance"],
                    budget_id=c.get("budget_id"),
                )
                for c in raw_chunks
            ]
            if not chunks:
                return {"estimate": _insufficient(
                    "No historical budgets crossed the relevance threshold for any component."
                ), "kept_chunks": []}

            encoder = get_token_encoder()
            kept = truncate_to_token_budget(chunks, settings.MAX_CONTEXT_TOKENS, encoder)
            context_block = build_context_block(kept)
            query = EstimationQuery.model_validate(structured_query)
            estimate = await rag_generate(context_block, structured_query=query)
            return {
                "estimate": estimate.model_dump(mode="json"),
                "kept_chunks": [c.model_dump(mode="json") for c in kept],
            }

        async def _validate_and_consolidate(
            estimate_dict: dict[str, Any],
            kept_chunks: list[dict[str, Any]],
            structured_query: dict[str, Any],
        ) -> dict[str, Any]:
            # Node 5: reuse validate_citations + check_coherence AS-IS (one
            # citation retry, one coherence repair -- same budget as the linear
            # pipeline). Unlike the linear pipeline, a still-broken estimate
            # after the repair attempt does NOT raise: an unhandled exception
            # mid-graph would crash the run instead of routing to a
            # needs_review end, so it's reported via ``validation_passed``
            # and the graph's conditional edge decides the final status.
            estimate = Estimate.model_validate(estimate_dict)
            kept = [RetrievedChunk.model_validate(c) for c in kept_chunks]
            if not kept:
                return {"estimate": estimate.model_dump(mode="json"), "validation_passed": True}

            query = EstimationQuery.model_validate(structured_query)
            context_block = build_context_block(kept)

            fabricated = validate_citations(estimate, kept)
            if fabricated:
                feedback = (
                    f"your previous response cited invalid source ids: {fabricated}. "
                    "Only cite ids that appear in the <sources> block."
                )
                estimate = await _generate(context_block, query, feedback=feedback)
                if validate_citations(estimate, kept):
                    estimate = estimate.model_copy(update={"confidence": "low"})

            if not check_coherence(estimate):
                feedback = (
                    'when confidence is "insufficient", total_engineer_days and '
                    "duration_weeks must be null, modules must be empty and "
                    "insufficient_context_explanation must be filled; otherwise provide "
                    "the modules, tasks and numbers."
                )
                estimate = await _generate(context_block, query, feedback=feedback)
                if not check_coherence(estimate):
                    return {
                        "estimate": estimate.model_dump(mode="json"),
                        "validation_passed": False,
                    }

            return {"estimate": estimate.model_dump(mode="json"), "validation_passed": True}

        deps = GraphDependencies(
            extract_requirements=_extract_requirements,
            classify_components=_classify_components,
            search_component_budgets=_search_component_budgets,
            generate_estimate=_generate_estimate,
            validate_and_consolidate=_validate_and_consolidate,
        )

        final_state = await run_estimation_graph(transcript, deps, idempotency_key)
        return Estimate.model_validate(final_state["estimate"])

    def _build_supervisor_deps(self) -> "SupervisorGraphDependencies":
        """Wire :class:`SupervisorGraphDependencies` -- exactly the four
        callables the tool-privilege table allows (one per specialist) -- to
        the real ``generation/rag`` tools. Shared by
        :meth:`estimate_from_transcript_multiagent` (first run) and
        :meth:`resume_multiagent_estimation` (continuation) so both drive the
        SAME compiled graph/dependency wiring:

            requirements_extractor -> LLM-only (reformulate + classify, no tool)
            budget_searcher        -> app.generation.rag.agent_tools.search_budgets
            estimate_generator     -> app.generation.rag.agent_tools.calculate_estimate
            coherence_validator    -> app.generation.rag.validation.validate_estimate

        Same layering constraint as ``estimate_from_transcript_graph``:
        ``generation/agentic/supervisor_graph.py`` cannot import
        ``generation/rag`` directly, so this is where the two compose.
        """
        from app.dependencies import get_llm_wrapper
        from app.generation.agentic.supervisor_graph import SupervisorGraphDependencies
        from app.generation.rag.agent_tools import calculate_estimate as rag_calculate_estimate
        from app.generation.rag.agent_tools import search_budgets as rag_search_budgets
        from app.generation.rag.errors import GenerationError
        from app.generation.rag.query_reformulator import reformulate_query
        from app.generation.rag.schemas import Estimate, EstimationQuery, RetrievedChunk
        from app.generation.rag.validation import validate_estimate as rag_validate_estimate

        settings = get_settings()

        async def _extract_requirements(transcript: str) -> dict[str, Any]:
            # Tool: none. Reuses the same reformulation + LLM classification
            # already proven in the Session 13 graph closures, bundled into
            # one callable per the Session 14 dependency contract.
            query = await reformulate_query(transcript)
            requirements = [query.function] if query.function else []
            requirements += [f"Use {tech}" for tech in query.technologies]
            requirements += query.constraints
            requirements += [f"Comply with {reg}" for reg in query.regulations]

            structured_query = query.model_dump(mode="json")
            wrapper = get_llm_wrapper()
            # ``complete_structured`` is sync; called bare inside this async
            # closure it would block the event loop for the whole LLM round
            # trip. Same fix as ``estimator.py::_generate`` for the identical
            # situation: push it to a worker thread and wrap failures the same
            # way every other pipeline failure is (caught as ``RagError`` by
            # the router).
            try:
                breakdown, _meta = await asyncio.to_thread(
                    wrapper.complete_structured,
                    system_prompt=(
                        "Group the given software requirements into functional "
                        "components (e.g. Auth, Payments, Data, Frontend, Infra). "
                        "Every requirement must belong to exactly one component."
                    ),
                    user_message=json.dumps(
                        {"requirements": requirements, "brief": structured_query}
                    ),
                    response_model=_ComponentBreakdown,
                    model_override=settings.REFORMULATION_MODEL,
                )
            except Exception as exc:  # noqa: BLE001
                raise GenerationError("Component classification failed.") from exc
            components = [c.model_dump(mode="json") for c in breakdown.components]
            return {
                "requirements": requirements,
                "structured_query": structured_query,
                "components": components,
            }

        async def _search_component_budgets(component: dict[str, Any]) -> dict[str, Any]:
            # Tool: search_budgets, reused as-is (S12).
            query_text = component.get("name", "") + ": " + "; ".join(
                component.get("requirements", [])
            )
            result = await rag_search_budgets(query_text)
            return {
                "component": component.get("name"),
                "chunks": result.get("items", []),
                "low_confidence": bool(result.get("low_confidence", not result.get("items"))),
            }

        def _calculate_estimate(components: list[dict[str, Any]]) -> dict[str, Any]:
            # Tool: calculate_estimate, reused as-is (S12) -- sync, no LLM.
            return rag_calculate_estimate(components)

        async def _validate_estimate(
            estimate_dict: dict[str, Any],
            kept_chunks: list[dict[str, Any]],
            structured_query: dict[str, Any],
        ) -> dict[str, Any]:
            # Tool: validate_estimate (new in this session, wraps the same
            # citation/coherence guards + one-retry budget as the linear graph).
            #
            # Incidental fix found during live pause/resume verification (not
            # one of the assigned Session 14 fixes, but a genuine pre-existing
            # crash that blocks every real run reaching this node): the dicts
            # in ``kept_chunks`` are the ``search_budgets`` agent-tool shape
            # (``chunk_id``, no ``chunk_type``), NOT the ``RetrievedChunk``
            # schema shape (``id``, mandatory ``chunk_type``) --
            # ``RetrievedChunk.model_validate(c)`` was raising a
            # ``ValidationError`` (missing ``id``/``chunk_type``) on every
            # non-empty ``kept_chunks``, an unhandled 500 the graph had never
            # actually exercised end-to-end before. Same field mapping already
            # used by ``estimate_from_transcript_graph``'s
            # ``_generate_estimate`` closure above, for the identical shape.
            estimate = Estimate.model_validate(estimate_dict)
            chunks = [
                RetrievedChunk(
                    id=c["chunk_id"],
                    content=c["content"],
                    sector=c["sector"],
                    project_year=c["project_year"],
                    chunk_type="historical_task",
                    distance=c["distance"],
                    budget_id=c.get("budget_id"),
                )
                for c in kept_chunks
            ]
            query = EstimationQuery.model_validate(structured_query)
            outcome = await rag_validate_estimate(estimate, chunks, query)
            return {
                "estimate": outcome["estimate"].model_dump(mode="json"),
                "validation_passed": outcome["validation_passed"],
                "confidence": outcome.get("confidence"),
            }

        return SupervisorGraphDependencies(
            extract_requirements=_extract_requirements,
            search_component_budgets=_search_component_budgets,
            calculate_estimate=_calculate_estimate,
            validate_estimate=_validate_estimate,
        )

    @staticmethod
    def _multiagent_outcome(final_state: dict[str, Any]) -> MultiagentEstimationOutcome:
        """Turn raw ``SupervisorState`` into the router's typed contract.

        ``final_state["__interrupt__"]`` is the tuple LangGraph's ``ainvoke``
        returns instead of a finished state when ``human_review_gate`` paused
        (see the mechanics comment on that node) -- its presence, not any
        business field, is what distinguishes "paused" from "done".
        """
        from app.generation.rag.schemas import Estimate

        thread_id = final_state["thread_id"]
        interrupts = final_state.get("__interrupt__")
        if interrupts:
            reasons = interrupts[0].value.get("reasons", [])
            return MultiagentEstimationOutcome(
                estimate=None,
                status="awaiting_human_review",
                estimation_id=thread_id,
                reasons=reasons,
            )
        return MultiagentEstimationOutcome(
            estimate=Estimate.model_validate(final_state["estimate"]),
            status=final_state.get("status", "validated"),
            estimation_id=thread_id,
        )

    async def estimate_from_transcript_multiagent(
        self,
        transcript: str,
        idempotency_key: str | None = None,
    ) -> MultiagentEstimationOutcome:
        """Supervisor/workers re-expression of the transcript pipeline (Session 14).

        Returns an ``awaiting_human_review`` outcome (no ``Estimate``) when
        ``human_review_gate`` pauses the graph; otherwise a finished one.
        """
        from app.generation.agentic.supervisor_graph import run_supervisor_graph

        deps = self._build_supervisor_deps()
        final_state = await run_supervisor_graph(transcript, deps, idempotency_key)
        return self._multiagent_outcome(final_state)

    async def resume_multiagent_estimation(
        self,
        estimation_id: str,
        decision: dict[str, Any],
    ) -> MultiagentEstimationOutcome:
        """Resume a paused multiagent run from its Postgres checkpoint.

        ``estimation_id`` is the checkpointer ``thread_id`` the caller got
        back from the paused first call (``AwaitingHumanReviewResponse``).
        """
        from app.generation.agentic.supervisor_graph import resume_supervisor_graph

        deps = self._build_supervisor_deps()
        final_state = await resume_supervisor_graph(estimation_id, decision, deps)
        return self._multiagent_outcome(final_state)
