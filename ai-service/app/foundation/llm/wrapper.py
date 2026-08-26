"""LiteLLM-backed wrapper that adds provider fallback, exact-match cache, cost tracking,
and structured logging to every LLM call in the estimator.

Design notes
------------
- The wrapper exposes two primitives:
  - ``complete()``: legacy free-text answer (kept for tests that depend on it).
  - ``complete_structured()``: returns a validated Pydantic model via Instructor,
    re-prompting on validator errors up to ``max_retries`` times.
- The Router is configured with two deployments under the same ``model_name``
  ("estimator") so LiteLLM can switch from primary to fallback transparently.
  When the caller overrides the model per-request we bypass the Router and call
  ``litellm.completion`` directly — that path has no fallback by design.
- ``primary_model``/``fallback_model`` are PROPERTIES backed by the runtime
  config store (Redis): the Settings UI can switch models mid-session. The
  Router keeps the *initial* (settings) deployments — it is never rebuilt at
  request time (thread-safety) — so an active runtime override routes through
  the direct ``litellm.completion`` path, with the same no-fallback semantics
  as a per-call ``model_override``.
"""

from __future__ import annotations

import time
from typing import Any, TypeVar

import instructor
import litellm
import structlog
from litellm import Router
from pydantic import BaseModel

from app.foundation.llm.runtime_config import RuntimeModelConfig
from app.foundation.observability.metrics import record_llm_call
from app.generation.cag.exact import EstimationCache

log = structlog.get_logger()


# Cost per 1M tokens (USD). Update as pricing changes.
MODEL_COSTS: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    # Session 9 — reasoning models used by the RAG estimation pipeline.
    "gpt-5": {"input": 1.25, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
}


T = TypeVar("T", bound=BaseModel)


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    base = _normalise_model_name(model)
    costs = MODEL_COSTS.get(base) or MODEL_COSTS.get(model) or {"input": 0.0, "output": 0.0}
    return round((tokens_in * costs["input"] + tokens_out * costs["output"]) / 1_000_000, 6)


def _normalise_model_name(model: str) -> str:
    """Strip provider prefixes like ``anthropic/`` that LiteLLM may emit."""
    return model.split("/", 1)[1] if "/" in model else model


def _provider_from_model(model: str) -> str:
    name = _normalise_model_name(model).lower()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith("gpt") or name.startswith("o1") or name.startswith("o3"):
        return "openai"
    return "unknown"


def _account_usage(raw: Any, model: str) -> dict[str, Any]:
    """Read token usage off Instructor's raw completion, price it, and record it.

    Instructor hands back the validated Pydantic model; the provider's ``usage``
    survives only if you ask for the raw completion alongside it
    (``create_with_completion``). Until Session 16 the two structured methods
    below discarded it — so the whole RAG pipeline, which runs through them,
    reported neither tokens nor cost, and every ``TurnObservation`` was persisted
    with ``cost_usd=0.0``. A missing number is a gap; a zero is a lie.

    Returns the three keys ``meta`` gains (``tokens_in`` / ``tokens_out`` /
    ``cost_usd``) and, as a side effect, adds the call to the per-request
    accumulator. Outside an HTTP request that side effect is a no-op.

    LIMIT, stated rather than papered over: when Instructor re-prompts on a
    validation error, ``usage`` describes the FINAL attempt, not the sum of the
    attempts. A heavily retried call therefore under-reports.
    """
    usage = getattr(raw, "usage", None)
    tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
    # The provider echoes the model it actually served, which may differ from the
    # alias we asked for; price what was served.
    served = _normalise_model_name(getattr(raw, "model", None) or model)
    cost_usd = _estimate_cost(served, tokens_in, tokens_out)
    record_llm_call(tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd)
    return {"tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost_usd}


class LLMWrapper:
    """Unified LLM client with cache, fallback, and cost tracking."""

    def __init__(
        self,
        *,
        openai_api_key: str | None,
        anthropic_api_key: str | None,
        primary_model: str,
        fallback_model: str,
        timeout: int,
        num_retries: int,
        cache: EstimationCache,
        runtime_config: RuntimeModelConfig | None = None,
    ):
        self.openai_api_key = openai_api_key
        self.anthropic_api_key = anthropic_api_key
        # Initial (settings) models: they seed the Router deployments and act
        # as the fallback when no runtime config store is wired (tests).
        self._initial_primary_model = primary_model
        self._initial_fallback_model = fallback_model
        self._runtime_config = runtime_config
        self.timeout = timeout
        self.num_retries = num_retries
        self.cache = cache

        self.router = Router(
            model_list=[
                {
                    "model_name": "estimator",
                    "litellm_params": {
                        "model": primary_model,
                        "api_key": openai_api_key,
                        "timeout": timeout,
                    },
                },
                {
                    "model_name": "estimator",
                    "litellm_params": {
                        "model": fallback_model,
                        "api_key": anthropic_api_key,
                        "timeout": timeout,
                    },
                },
            ],
            fallbacks=[{"estimator": ["estimator"]}],
            num_retries=num_retries,
        )

        # Instructor wraps ``litellm.completion`` so we can call any of the
        # underlying providers with the same ``response_model=`` API.
        self._instructor = instructor.from_litellm(litellm.completion)

    # ------------------------------------------------------------------
    # Effective models (runtime override > settings default)
    # ------------------------------------------------------------------

    @property
    def primary_model(self) -> str:
        """The model every non-overridden call uses, resolved per call so a
        runtime override (Settings UI) takes effect immediately."""
        if self._runtime_config is not None:
            return self._runtime_config.effective("PRIMARY_MODEL")
        return self._initial_primary_model

    @property
    def fallback_model(self) -> str:
        if self._runtime_config is not None:
            return self._runtime_config.effective("FALLBACK_MODEL")
        return self._initial_fallback_model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def complete(
        self,
        *,
        system_prompt: str,
        user_message: str,
        model_override: str | None = None,
        max_tokens: int = 4000,
        thinking_budget: int | None = None,
    ) -> dict[str, Any]:
        """Single LLM call returning a free-text answer. Kept for tests."""
        cache_key_model = model_override or self.primary_model
        cache_key = EstimationCache.make_key(
            system_prompt=system_prompt,
            user_message=user_message,
            model=cache_key_model,
            max_tokens=max_tokens,
            thinking_budget=thinking_budget,
        )
        cached = self.cache.get(cache_key)
        if cached:
            return {**cached, "cache_hit": True}

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        kwargs = self._build_call_kwargs(
            messages=messages,
            max_tokens=max_tokens,
            thinking_budget=thinking_budget,
            model_override=model_override,
        )

        log.info(
            "llm_call_started",
            mode="blocking",
            model=model_override or self.primary_model,
        )
        t0 = time.perf_counter()
        try:
            response = self._dispatch(model_override=model_override, **kwargs)
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            log.error(
                "llm_call_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                latency_ms=latency_ms,
            )
            raise

        latency_ms = int((time.perf_counter() - t0) * 1000)
        result = self._normalise_response(response, latency_ms=latency_ms)
        # This path already priced itself; it just never told anyone but the log.
        # A cache hit returns earlier and is deliberately NOT recorded: nothing
        # was spent, and counting it would make the cache look expensive.
        record_llm_call(
            tokens_in=result["usage"]["input_tokens"],
            tokens_out=result["usage"]["output_tokens"],
            cost_usd=result["cost_usd"],
        )
        log.info(
            "llm_call_completed",
            model=result["model"],
            provider=result["provider"],
            input_tokens=result["usage"]["input_tokens"],
            output_tokens=result["usage"]["output_tokens"],
            cost_usd=result["cost_usd"],
            latency_ms=latency_ms,
            finish_reason=result["finish_reason"],
        )
        self.cache.set(cache_key, result)
        return {**result, "cache_hit": False}

    def complete_structured_chat(
        self,
        *,
        messages: list[dict[str, str]],
        response_model: type[T],
        model_override: str | None = None,
        max_tokens: int = 4000,
        max_retries: int = 6,
        reasoning_effort: str | None = None,
    ) -> tuple[T, dict[str, Any]]:
        """Conversational variant of :meth:`complete_structured`.

        Accepts a pre-built ``messages`` list (system + N user/assistant pairs +
        current user). Bypasses the Router for deterministic routing — same
        rationale as ``complete_structured``: the LiteLLM Router would
        round-robin between deployments and could non-deterministically pick
        the fallback. Instructor handles re-prompts when Pydantic validators
        raise.
        """
        target_model = model_override or self.primary_model
        api_key = (
            self.anthropic_api_key
            if _provider_from_model(target_model) == "anthropic"
            else self.openai_api_key
        )

        log.info(
            "llm_structured_chat_started",
            model=target_model,
            response_model=response_model.__name__,
            messages=len(messages),
        )
        # Reasoning models (gpt-5 family) accept ``reasoning_effort``; LiteLLM
        # forwards it and translates ``max_tokens`` to ``max_completion_tokens``.
        extra: dict[str, Any] = {}
        if reasoning_effort is not None:
            extra["reasoning_effort"] = reasoning_effort

        t0 = time.perf_counter()
        try:
            # ``create_with_completion``, not ``chat.completions.create``: the
            # second returns only the parsed model and the provider's token
            # usage dies with the response object.
            result, raw = self._instructor.create_with_completion(
                model=target_model,
                api_key=api_key,
                timeout=self.timeout,
                messages=messages,
                response_model=response_model,
                max_tokens=max_tokens,
                max_retries=max_retries,
                **extra,
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            log.error(
                "llm_structured_chat_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                latency_ms=latency_ms,
            )
            raise

        latency_ms = int((time.perf_counter() - t0) * 1000)
        meta = {
            "model": _normalise_model_name(target_model),
            "provider": _provider_from_model(target_model),
            "latency_ms": latency_ms,
            **_account_usage(raw, target_model),
        }
        log.info(
            "llm_structured_chat_completed",
            model=meta["model"],
            provider=meta["provider"],
            latency_ms=latency_ms,
            tokens_in=meta["tokens_in"],
            tokens_out=meta["tokens_out"],
            cost_usd=meta["cost_usd"],
        )
        return result, meta

    def complete_structured(
        self,
        *,
        system_prompt: str,
        user_message: str,
        response_model: type[T],
        model_override: str | None = None,
        max_tokens: int = 4000,
        max_retries: int = 6,
        reasoning_effort: str | None = None,
    ) -> tuple[T, dict[str, Any]]:
        """Run the LLM with Instructor and return ``(model_instance, meta)``.

        ``meta`` includes ``model``, ``provider`` and ``latency_ms``. Instructor
        re-prompts the LLM up to ``max_retries`` times when a Pydantic validator
        raises, feeding the ``ValueError`` message back to the model.

        Streaming bypasses are not relevant here — the entire model is built
        atomically by Instructor before this function returns.
        """
        target_model = model_override or self.primary_model
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        api_key = (
            self.anthropic_api_key
            if _provider_from_model(target_model) == "anthropic"
            else self.openai_api_key
        )

        log.info(
            "llm_structured_call_started",
            model=target_model,
            response_model=response_model.__name__,
        )
        extra: dict[str, Any] = {}
        if reasoning_effort is not None:
            extra["reasoning_effort"] = reasoning_effort

        t0 = time.perf_counter()
        try:
            # ``create_with_completion``, not ``chat.completions.create``: the
            # second returns only the parsed model and the provider's token
            # usage dies with the response object.
            result, raw = self._instructor.create_with_completion(
                model=target_model,
                api_key=api_key,
                timeout=self.timeout,
                messages=messages,
                response_model=response_model,
                max_tokens=max_tokens,
                max_retries=max_retries,
                **extra,
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            log.error(
                "llm_structured_call_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                latency_ms=latency_ms,
            )
            raise

        latency_ms = int((time.perf_counter() - t0) * 1000)
        meta = {
            "model": _normalise_model_name(target_model),
            "provider": _provider_from_model(target_model),
            "latency_ms": latency_ms,
            # ``tokens_in`` / ``tokens_out`` / ``cost_usd``. The names are not
            # free: ``TurnObservation`` in the estimation service has been
            # reading exactly these three keys since Session 5, and getting
            # nothing.
            **_account_usage(raw, target_model),
        }
        log.info(
            "llm_structured_call_completed",
            model=meta["model"],
            provider=meta["provider"],
            latency_ms=latency_ms,
            tokens_in=meta["tokens_in"],
            tokens_out=meta["tokens_out"],
            cost_usd=meta["cost_usd"],
        )
        return result, meta

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_call_kwargs(
        self,
        *,
        messages: list[dict],
        max_tokens: int,
        thinking_budget: int | None,
        model_override: str | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
        }

        if thinking_budget is not None:
            target_model = model_override or self.primary_model
            if _provider_from_model(target_model) == "anthropic":
                kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
                kwargs["max_tokens"] = max(max_tokens, thinking_budget + 1024)
            else:
                log.warning(
                    "thinking_budget_ignored_for_provider",
                    provider=_provider_from_model(target_model),
                    model=target_model,
                )
        return kwargs

    def _dispatch(self, *, model_override: str | None, **kwargs: Any) -> Any:
        """Call the Router (with fallback) or LiteLLM directly when the caller
        wants a specific model.

        A runtime primary override behaves like a per-call ``model_override``:
        the Router deployments are frozen at construction (never rebuilt at
        request time), so when the effective primary differs from the one the
        Router was built with, the call goes direct — no automatic fallback.
        """
        if model_override:
            return self._direct_completion(model=model_override, **kwargs)
        effective_primary = self.primary_model
        if effective_primary != self._initial_primary_model:
            return self._direct_completion(model=effective_primary, **kwargs)
        return self.router.completion(model="estimator", **kwargs)

    def _direct_completion(self, *, model: str, **kwargs: Any) -> Any:
        api_key = (
            self.anthropic_api_key
            if _provider_from_model(model) == "anthropic"
            else self.openai_api_key
        )
        return litellm.completion(
            model=model,
            api_key=api_key,
            timeout=self.timeout,
            num_retries=self.num_retries,
            **kwargs,
        )

    @staticmethod
    def _normalise_response(response: Any, *, latency_ms: int) -> dict[str, Any]:
        choice = response.choices[0]
        finish_reason = (choice.finish_reason or "stop").lower()
        usage = response.usage
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        total_tokens = getattr(usage, "total_tokens", input_tokens + output_tokens) or (
            input_tokens + output_tokens
        )

        model = _normalise_model_name(response.model)
        return {
            "estimation": choice.message.content or "",
            "model": model,
            "provider": _provider_from_model(model),
            "finish_reason": finish_reason,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
            "latency_ms": latency_ms,
            "cost_usd": _estimate_cost(model, input_tokens, output_tokens),
        }
