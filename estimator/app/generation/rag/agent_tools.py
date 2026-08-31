"""Agent tools for agentic estimation (Session 12).

Two tools wrapped for the Responses API: search_budgets (retrieval) and
calculate_estimate (deterministic aggregation). Both are stateless functions
that the agent invokes via function calling.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from app.generation.rag.schemas import Estimate, EstimationQuery, RetrievedChunk, WorkModule

log = structlog.get_logger()


async def search_budgets(query: str, filters: dict | None = None) -> dict[str, Any]:
    """Search historical budgets matching a component description.

    Wraps the S9-S10 retrieval pipeline (vector + hybrid + reranking).
    Called multiple times during the agent loop for different components.

    Parameters
    ----------
    query : str
        Component or requirement description (e.g., "backend business logic",
        "mobile app integration").
    filters : dict, optional
        Filtering criteria: component_type, date_range, sector, etc.

    Returns
    -------
    dict
        {
            "items": [
                {
                    "chunk_id": int,
                    "content": str,
                    "sector": str,
                    "project_year": int,
                    "distance": float,
                    "budget_id": str
                },
                ...
            ],
            "count": int,
            "query_used": str
        }
    """
    from app.dependencies import get_embedder, get_runtime_retrieval_config, get_settings, get_token_encoder
    from app.generation.rag.context_assembler import truncate_to_token_budget
    from app.generation.rag.query_reformulator import compose_search_text
    from app.generation.rag.retrieval.pipeline import retrieve

    settings = get_settings()
    embedder = get_embedder()
    encoder = get_token_encoder()
    runtime_retrieval = get_runtime_retrieval_config()

    if embedder is None:
        return {"items": [], "count": 0, "error": "Embedding service unavailable"}

    try:
        # Embed the query (embed_one is synchronous, run in thread)
        query_embedding = await asyncio.to_thread(embedder.embed_one, query)

        # Retrieve with current configuration
        search_mode = runtime_retrieval.effective_search_mode()
        rerank = runtime_retrieval.effective_rerank()
        sector = filters.get("sector") if filters else None
        sectors = [sector] if sector else None

        retrieval = await retrieve(
            query_embedding=query_embedding,
            query_text=query,
            search_mode=search_mode,
            rerank=rerank,
            top_k=settings.RETRIEVAL_TOP_K,
            recall_k=settings.RETRIEVAL_RECALL_TOP_K,
            rerank_top_n=settings.RERANK_TOP_N,
            distance_threshold=settings.RETRIEVAL_DISTANCE_THRESHOLD,
            rrf_k=settings.RRF_K,
            sectors=sectors,
        )

        # Truncate to budget
        kept = truncate_to_token_budget(retrieval.chunks, settings.MAX_CONTEXT_TOKENS, encoder)

        # Format for agent consumption
        items = [
            {
                "chunk_id": chunk.id,
                "content": chunk.content,
                "sector": chunk.sector,
                "project_year": chunk.project_year,
                "distance": float(chunk.distance),
                "budget_id": chunk.budget_id,
            }
            for chunk in kept
        ]

        return {
            "items": items,
            "count": len(items),
            "query_used": query,
            "low_confidence": retrieval.low_confidence,
        }

    except Exception as exc:
        log.error("search_budgets_failed", error=str(exc)[:300])
        return {"items": [], "count": 0, "error": str(exc)[:200]}


def calculate_estimate(components: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate partial or total estimate from component references.

    Deterministic aggregation: no LLM. Takes identified components and their
    historical reference amounts, computes totals and breakdowns.

    Parameters
    ----------
    components : list[dict]
        Each item: {"name": str, "reference_amounts": list[int] or {...}, ...}

    Returns
    -------
    dict
        {
            "total_engineer_days": int,
            "breakdown": [
                {"component": str, "estimated_days": int, "confidence": str},
                ...
            ],
            "summary": str
        }
    """
    try:
        total_days = 0
        breakdown = []

        for comp in components:
            comp_name = comp.get("name", "unknown")
            ref_amounts = comp.get("reference_amounts", [])

            # If ref_amounts is a dict (e.g., {min, max, median}), extract median
            if isinstance(ref_amounts, dict):
                estimated = ref_amounts.get("median") or ref_amounts.get("mean") or 0
            # If it's a list, take the median
            elif isinstance(ref_amounts, list) and ref_amounts:
                sorted_amounts = sorted(ref_amounts)
                estimated = sorted_amounts[len(sorted_amounts) // 2]
            else:
                estimated = 0

            total_days += estimated
            breakdown.append(
                {
                    "component": comp_name,
                    "estimated_days": int(estimated),
                    "confidence": "medium" if estimated > 0 else "low",
                }
            )

        return {
            "total_engineer_days": total_days,
            "breakdown": breakdown,
            "summary": f"Estimated {total_days} engineer-days across {len(components)} components",
        }

    except Exception as exc:
        log.error("calculate_estimate_failed", error=str(exc)[:300])
        return {
            "total_engineer_days": 0,
            "breakdown": [],
            "summary": f"Calculation failed: {str(exc)[:100]}",
        }
