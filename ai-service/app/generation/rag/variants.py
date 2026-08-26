"""Session 16 — which version of the system serves this request.

An A/B test on an LLM system compares two things at once, and both have to be on
the same table or the result is meaningless:

* **quality** — does B still estimate as well? (the golden set answers this)
* **cost**   — is B actually cheaper? (the per-request metrics answer this)

A variant that is 60% cheaper and estimates worse is not an improvement, and a
variant that is 5% cheaper and identical is not worth the operational weight of
having two code paths. Only the two columns together decide.

WHAT B IS. The cost experiment: a small model for the generation step plus the
embedding cache. Nothing else. It is tempting to fold in every improvement at once
— including the prompt fix the pre-exercise found — and it is exactly the mistake
that makes a result unusable: when the numbers move you cannot say which change
moved them. One variable per experiment. The prompt fix gets its own run.

HOW A REQUEST IS ASSIGNED. By hashing the request id, not by drawing a random
number. Two reasons, and the second is the one that bites:

1. A retry of the same request lands in the same bucket, so an idempotent replay
   does not silently switch arms mid-experiment.
2. It is reproducible. Given the trace of a request you can recompute which arm it
   was in, months later, without having stored anything.

``X-Variant: a|b`` overrides the split entirely. That is for the demo and for
debugging a specific arm — it is not part of the experiment, and forced requests
are labelled ``forced`` so they can be excluded from the comparison.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

import structlog

from app.config import Settings, get_settings

log = structlog.get_logger()

Variant = Literal["a", "b"]

__all__ = ["Variant", "VariantPlan", "assign_variant", "plan_for"]


@dataclass(frozen=True)
class VariantPlan:
    """What this variant actually changes. Data, not branches.

    Everything a variant does is a value the pipeline reads. That keeps the
    difference between arms auditable in one place instead of scattered across
    ``if variant == "b"`` down the call stack — which is how A/B tests rot into
    two subtly divergent systems nobody dares delete.
    """

    variant: Variant
    generation_model: str | None  # None = the configured default
    reasoning_effort: str | None
    embedding_cache: bool
    forced: bool = False

    def as_labels(self) -> dict:
        return {"variant": self.variant, "variant_forced": self.forced}


def assign_variant(
    request_id: str,
    *,
    percent_b: int,
    enabled: bool,
) -> Variant:
    """Deterministic bucket for ``request_id``. Same id, same arm, always.

    The hash is taken over the id alone, so the assignment survives a restart, a
    redeploy and a different replica — none of which is true of a random draw.
    """
    if not enabled or percent_b <= 0:
        return "a"
    if percent_b >= 100:
        return "b"
    digest = hashlib.sha256(request_id.encode("utf-8")).digest()
    # Two bytes → 0..65535 → 0..99. Plenty of resolution for a percentage, and
    # cheap enough to run on every request.
    bucket = int.from_bytes(digest[:2], "big") % 100
    return "b" if bucket < percent_b else "a"


def plan_for(
    request_id: str,
    *,
    forced: Variant | None = None,
    settings: Settings | None = None,
    percent_b: int | None = None,
    enabled: bool | None = None,
) -> VariantPlan:
    """Resolve the variant and turn it into the values the pipeline will read."""
    settings = settings or get_settings()
    if forced is not None:
        variant: Variant = forced
    else:
        variant = assign_variant(
            request_id,
            percent_b=settings.AB_VARIANT_B_PERCENT if percent_b is None else percent_b,
            enabled=settings.AB_TESTING_ENABLED if enabled is None else enabled,
        )

    if variant == "b":
        return VariantPlan(
            variant="b",
            generation_model=settings.AB_VARIANT_B_GENERATION_MODEL,
            reasoning_effort=settings.AB_VARIANT_B_REASONING_EFFORT,
            embedding_cache=True,
            forced=forced is not None,
        )
    return VariantPlan(
        variant="a",
        generation_model=None,
        reasoning_effort=None,
        embedding_cache=settings.EMBEDDING_CACHE_ENABLED,
        forced=forced is not None,
    )
