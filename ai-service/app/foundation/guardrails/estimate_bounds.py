"""Deterministic bounds on an estimate's magnitude (Session 16).

A guardrail is CODE, not a sentence in a prompt. This module holds the arithmetic
that decides whether a number the model produced is defensible given the evidence
the model was shown — and it runs whatever the model did.

WHY THIS EXISTS. The Session 16 pre-exercise ran the golden set against the
deployed system and found the same transcript coming back as **566 engineer-days
on one call and 82 on the next**. The cause is in the generation prompt, which
never states how many hours make a day while the sources are in hours and the
schema field is in days. Fixing the prompt is one answer; it is also the answer
that depends on the model behaving. This is the other answer: 566 days against
roughly 77 days of retrieved evidence is a claim that does not stand up, and
noticing that takes a division, not a language model.

WHERE THE LIMIT COMES FROM. Not a magic constant. The retrieved chunks carry the
historical ``estimated_hours`` the model read; their sum, converted to days, is
the evidence base. An estimate may legitimately exceed it — the project can be
bigger than its analogs — but not by an arbitrary factor. With the real numbers
from that run: evidence ≈ 77 days, the good answer 82 (1.1x) and the bad one 566
(7.4x). The default ceiling of 3x separates them with room to spare, and still
catches the failure mode that produced it, which is an 8x unit conflation.

LINEAGE. The per-component version of this rule already exists twice in the repo:
``generation/agentic/agent_tools.py::validate_estimate`` (Session 12) and a
days-based port in ``domain/graph/nodes.py::_validate`` (Session 13/14). This
module is where the shared arithmetic now lives; ``validate_estimate`` delegates
to it. ``_validate`` keeps its own loop because it works over a different shape
(dicts plus ``BudgetMatch``) and is Session 13/14 teaching material — the
duplication is named here rather than left to be discovered.

It lives in ``foundation`` because two different layers need it and they may not
import each other: the RAG estimate path (``generation/rag``) and the graph
(``domain/graph``). It therefore speaks only in numbers — no ``Estimate``, no
``RetrievedChunk`` — and each caller adapts its own types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.config import Settings, get_settings

__all__ = [
    "HOURS_PER_DAY",
    "BoundsVerdict",
    "check_total_bounds",
    "plausible_range",
]

# One working day = 8 engineer-hours, the same convention the graph uses.
HOURS_PER_DAY = 8.0


def plausible_range(references: Sequence[float]) -> tuple[float, float] | None:
    """The band a line may fall in given its historical references.

    ``[min * 0.5, max * 2.0]`` — deliberately wide. The job of this band is to
    catch a number that is wrong by an order of magnitude, not to second-guess an
    estimator who scoped something 30% high. A tight band would fire constantly,
    everyone would learn to ignore it, and the guardrail would be decoration.

    ``None`` when there are no references: absence of evidence is a different
    finding from evidence of implausibility, and the caller should say so.
    """
    values = [float(r) for r in references if r is not None]
    if not values:
        return None
    return min(values) * 0.5, max(values) * 2.0


@dataclass(frozen=True)
class BoundsVerdict:
    """The outcome. ``ok`` False means "a person should look", never "discard"."""

    ok: bool
    reasons: list[str] = field(default_factory=list)
    total_engineer_days: int | None = None
    evidence_engineer_days: float = 0.0
    ratio: float | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reasons": list(self.reasons),
            "total_engineer_days": self.total_engineer_days,
            "evidence_engineer_days": round(self.evidence_engineer_days, 1),
            "ratio": None if self.ratio is None else round(self.ratio, 2),
        }


def check_total_bounds(
    total_engineer_days: int | None,
    evidence_hours: Sequence[int | None],
    *,
    settings: Settings | None = None,
    max_evidence_ratio: float | None = None,
    evidence_description: str = "retrieved evidence",
) -> BoundsVerdict:
    """Is this total defensible given the evidence that produced it?

    ``total_engineer_days`` of ``None`` is an ABSTENTION, and abstention is the
    system working. It passes.

    ``max_evidence_ratio`` and ``evidence_description`` exist because the same
    arithmetic answers two different questions. Where the model INVENTS the total
    (``/v1/estimate/from-transcript``) the ratio catches a fabricated number, and
    the default 3x applies. Where the total is DERIVED from the evidence — the
    per-task consensus of the wizard and the graph — it cannot be fabricated, and
    what the ratio really measures is how many times the same historical analog
    was reused. That is worth flagging too, at a looser bound and in different
    words: a reason that names the wrong failure teaches the reviewer to distrust
    the guardrail.
    """
    settings = settings or get_settings()
    if max_evidence_ratio is None:
        max_evidence_ratio = settings.ESTIMATE_MAX_EVIDENCE_RATIO

    if total_engineer_days is None:
        return BoundsVerdict(ok=True, total_engineer_days=None)

    hours = [float(h) for h in evidence_hours if h]
    evidence_days = sum(hours) / HOURS_PER_DAY
    reasons: list[str] = []

    if total_engineer_days <= 0:
        reasons.append(f"total of {total_engineer_days} engineer-days is not a positive number")

    if total_engineer_days > settings.ESTIMATE_MAX_ENGINEER_DAYS:
        reasons.append(
            f"total of {total_engineer_days} engineer-days exceeds the absolute ceiling of "
            f"{settings.ESTIMATE_MAX_ENGINEER_DAYS} (~10 person-years for one project)"
        )

    ratio: float | None = None
    if evidence_days > 0:
        ratio = total_engineer_days / evidence_days
        if ratio > max_evidence_ratio:
            reasons.append(
                f"total of {total_engineer_days} engineer-days is {ratio:.1f}x the "
                f"{evidence_days:.0f} engineer-days of {evidence_description}, above the "
                f"{max_evidence_ratio:.1f}x limit"
            )
    elif total_engineer_days > 0:
        # A number with nothing behind it. Distinct from an implausible number,
        # and worth its own wording so the reviewer knows which one they have.
        reasons.append(
            f"total of {total_engineer_days} engineer-days rests on no retrieved "
            "evidence carrying hours"
        )

    return BoundsVerdict(
        ok=not reasons,
        reasons=reasons,
        total_engineer_days=total_engineer_days,
        evidence_engineer_days=evidence_days,
        ratio=ratio,
    )
