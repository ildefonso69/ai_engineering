"""Pure helpers shared by the multi-agent nodes.

Kept free of I/O so they are trivially unit-testable and can be reused by the
fan-out branch, the recovery join and the estimate builder without duplicating the
module→task bookkeeping.
"""

from __future__ import annotations

from app.config import Settings, get_settings
from app.foundation.guardrails.estimate_bounds import HOURS_PER_DAY, check_total_bounds

# A grounded task below this reliability is doubtful enough to hand to the recovery
# agent (mirrors ``app/domain/agent_estimation.py::_LOW_RELIABILITY``).
LOW_RELIABILITY = 0.35


def modules_from_structure(structure: dict | None) -> list[dict]:
    """``AgentStructure`` dump → ``TaskHoursModuleInput``-shaped list of dicts.

    ``[{"name": ..., "tasks": [{"name": ..., "description": ...}]}]`` — the shape the
    human gate hands to the fan-out and that ``estimate_one`` consumes per task.
    """
    modules: list[dict] = []
    for module in (structure or {}).get("modules") or []:
        modules.append(
            {
                "name": module.get("name"),
                "tasks": [
                    {"name": task.get("name"), "description": task.get("description")}
                    for task in (module.get("tasks") or [])
                    if task.get("name")
                ],
            }
        )
    return modules


def flag_reason(task_hours: dict) -> str | None:
    """Why (if at all) a per-task hours row is worth agentic recovery.

    Mirrors ``agent_estimation._flag_reason`` but over the plain dict the fan-out
    accumulated: no match / contradictory range / low reliability.
    """
    if not task_hours.get("has_match"):
        return "no historical analog under the distance threshold"
    if task_hours.get("hours_range") is not None:
        return "historical analogs contradict (a range, not a point)"
    reliability = task_hours.get("reliability")
    if reliability is not None and reliability < LOW_RELIABILITY:
        return f"low reliability ({reliability})"
    return None


def recompute_estimate_totals(modules: list[dict]) -> dict:
    """The four headline totals derived from a module→task tree's ``estimated_hours``.

    A task is "grounded" when its ``estimated_hours`` is not ``None`` (so a human who
    fills a previously-unmatched task at gate 2 grounds it). Shared by ``build_estimate``
    and the gate-2 override path so the arithmetic lives in exactly one place.
    """
    total_hours = 0.0
    grounded = 0
    total_tasks = 0
    for module in modules or []:
        for task in module.get("tasks") or []:
            total_tasks += 1
            hours = task.get("estimated_hours")
            if hours is not None:
                total_hours += hours
                grounded += 1

    ratio = round(grounded / total_tasks, 3) if total_tasks else 0.0
    if total_tasks and grounded == total_tasks:
        confidence = "high"
    elif grounded == 0:
        confidence = "low"
    else:
        confidence = "medium"
    return {
        "total_engineer_hours": round(total_hours, 1),
        "total_engineer_days": round(total_hours / HOURS_PER_DAY),
        "grounded_task_ratio": ratio,
        "confidence": confidence,
    }


def evidence_hours_from_task_hours(task_hours: list[dict]) -> list[int]:
    """Historical hours behind the graph's estimate, counted ONCE per source chunk.

    The dict-shaped twin of ``generation/rag/guardrails.py::neighbor_evidence_hours``
    — same rule, different shape, because by the time the fan-out has joined, the
    state carries ``TaskHoursEstimate.model_dump()`` dicts rather than the models.
    Naming the duplication here rather than leaving it to be discovered: the
    arithmetic that matters (``check_total_bounds``) is shared; only the six lines
    that walk the shape are not.
    """
    seen: dict[int, int] = {}
    for row in task_hours or []:
        for neighbor in row.get("neighbors") or []:
            source_id = neighbor.get("source_id")
            hours = neighbor.get("estimated_hours")
            if source_id is not None and hours and source_id not in seen:
                seen[source_id] = int(hours)
    return list(seen.values())


def review_fields(
    modules: list[dict],
    task_hours: list[dict],
    totals: dict,
    *,
    settings: Settings | None = None,
) -> dict:
    """The Session 16 output guardrail over a graph estimate.

    Returns the two fields the business backend routes on. Deliberately the same
    names as ``Estimate.requires_human_review`` / ``review_reasons``: the platform
    should not need to know which flow produced a breakdown in order to decide
    whether a person should see it.

    Note which notion of "grounded" this uses. The wizard counts ``has_match``,
    because at that point nobody has intervened. Here it counts tasks that ended
    up with hours, because a human filling a previously-unmatched task at gate 2
    HAS grounded it — the same rule ``recompute_estimate_totals`` already applies
    to ``grounded_task_ratio``.
    """
    settings = settings or get_settings()
    if not settings.ESTIMATE_BOUNDS_ENABLED:
        return {"requires_human_review": False, "review_reasons": []}

    total_tasks = sum(len(module.get("tasks") or []) for module in modules or [])
    grounded = sum(
        1
        for module in modules or []
        for task in module.get("tasks") or []
        if task.get("estimated_hours") is not None
    )

    evidence = evidence_hours_from_task_hours(task_hours)
    # No task carries hours ⇒ an abstention, which passes the bound. The
    # ungrounded check below is what explains an empty estimate; "0 engineer-days
    # is not a positive number" would just be noise beside it.
    total_days = totals.get("total_engineer_days") if grounded else None

    verdict = check_total_bounds(
        total_days,
        evidence,
        settings=settings,
        # The graph derives its hours from the neighbours, so the ratio measures
        # analog REUSE, not a fabricated total. Different question, looser bound.
        max_evidence_ratio=settings.TASK_HOURS_MAX_EVIDENCE_RATIO,
        evidence_description=f"{len(evidence)} distinct historical analogs",
    )
    reasons = list(verdict.reasons)

    ungrounded = total_tasks - grounded
    if total_tasks and ungrounded > total_tasks / 2:
        reasons.append(f"{ungrounded} of {total_tasks} tasks have no hours behind them")

    return {"requires_human_review": bool(reasons), "review_reasons": reasons}


def build_estimate(approved_modules: list[dict], task_hours: list[dict]) -> dict:
    """Assemble the structured estimate from the approved tree + per-task hours.

    Walks the human-approved module→task tree and grafts each task's grounded hours
    (matched by ``(module, task)``), then sums to totals. A task with no match keeps
    ``estimated_hours=None`` (flagged for the human at gate 2).
    """
    by_key = {(t.get("module"), t.get("task")): t for t in task_hours}
    out_modules: list[dict] = []
    for module in approved_modules:
        tasks_out: list[dict] = []
        for task in module.get("tasks") or []:
            est = by_key.get((module.get("name"), task.get("name")))
            tasks_out.append(
                {
                    "name": task.get("name"),
                    "description": task.get("description"),
                    "estimated_hours": est.get("estimated_hours") if est else None,
                    "reliability": est.get("reliability") if est else None,
                    "has_match": bool(est and est.get("has_match")),
                }
            )
        out_modules.append({"name": module.get("name"), "tasks": tasks_out})

    totals = recompute_estimate_totals(out_modules)
    return {
        "modules": out_modules,
        **totals,
        **review_fields(out_modules, task_hours, totals),
    }
