"""``POST /v1/retrieval/hybrid-search`` — vector + lexical fusion via RRF.

Combines vector k-NN search and full-text keyword ranking into a single
fused ranking using Reciprocal Rank Fusion (RRF). Both branches see the
same structural filters (sector, year, chunk type) and are capped at
2×top_k to maximize overlap odds before fusion. The result is a single
ranking ordered by RRF score (highest first).

RRF is scale-agnostic and robust to different score ranges — it sees only
ranks. Results appearing in both branches score higher; RRF score = sum of
1 / (rrf_k + rank_in_branch) for each branch where the chunk appears.

Auth: ``X-API-Key`` header with ``RETRIEVAL_API_KEY`` (same as ``/search``).
Rate limit: 120 requests/min (same budget as ``/search``).
"""

from __future__ import annotations

from datetime import date

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.rate_limiting import limiter
from app.api.security import require_retrieval_key
from app.dependencies import get_embedder, get_engine, get_settings
from app.foundation.reranking import CrossEncoderReranker, DEFAULT_RERANK_MODEL
from app.generation.rag.errors import RetrievalError
from app.generation.rag.schemas import RetrievedChunk, RetrievalResult
from app.generation.rag.store.models import (
    BudgetChunkRow,
    TechnicalDocChunkRow,
    TranscriptChunkRow,
)
from app.generation.rag.store.repository import ChunkStore
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

log = structlog.get_logger()

router = APIRouter(prefix="/v1/retrieval", tags=["retrieval"])

# Collection dispatch: corpus → table
COLLECTIONS = {
    "budget": BudgetChunkRow,
    "transcript": TranscriptChunkRow,
    "technical_doc": TechnicalDocChunkRow,
}


class HybridSearchRequest(BaseModel):
    """Payload for ``POST /v1/retrieval/hybrid-search``."""

    query_text: str = Field(min_length=5, max_length=2000, description="Text to search")
    collection: str = Field(
        default="budget",
        description="Chunk collection: 'budget', 'transcript', or 'technical_doc'",
    )
    top_k: int = Field(default=10, ge=1, le=30, description="Results to return")
    distance_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Cosine distance threshold for vector branch (lower = stricter)",
    )
    sectors: list[str] | None = Field(
        default=None, description="Filter by client_sector (in JSONB metadata)"
    )
    year_min: int | None = Field(default=None, description="Filter by year >= this")
    year_max: int | None = Field(default=None, description="Filter by year <= this")
    chunk_types: list[str] | None = Field(
        default=None, description="Filter by chunk_type (e.g., budget_component)"
    )
    rrf_k: float = Field(
        default=60.0,
        ge=1.0,
        le=500.0,
        description="RRF smoothing constant; higher = more balanced fusion, lower = favor top ranks",
    )
    enable_reranking: bool = Field(
        default=False,
        description="If true, apply cross-encoder reranking to top-50 candidates, return top-rerank_top_k",
    )
    rerank_model: str = Field(
        default=DEFAULT_RERANK_MODEL,
        description="Cross-encoder model name (HuggingFace); only used if enable_reranking=true",
    )
    rerank_top_k: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Final results after reranking; only used if enable_reranking=true",
    )


class HybridSearchResult(BaseModel):
    """Single fused result with RRF score and optional rerank score."""

    chunk: RetrievedChunk
    rrf_score: float = Field(
        description="Reciprocal Rank Fusion score; sum of 1/(k+rank) across branches"
    )
    rerank_score: float | None = Field(
        default=None,
        description="Cross-encoder relevance score [0, 1] if reranking enabled, else None",
    )
    vector_rank: int | None = Field(default=None, description="Rank in vector branch (0-indexed), or None if missing")
    lexical_rank: int | None = Field(default=None, description="Rank in lexical branch (0-indexed), or None if missing")


class HybridSearchResponse(BaseModel):
    """Fused hybrid search result."""

    results: list[HybridSearchResult]
    query_text: str
    collection: str
    top_k: int
    rrf_k: float
    reranked: bool = Field(
        description="Whether cross-encoder reranking was applied"
    )
    rerank_model: str | None = Field(
        default=None, description="Cross-encoder model used, if reranked=true"
    )
    rerank_top_k: int | None = Field(
        default=None, description="Final top-k after reranking, if reranked=true"
    )


@router.post("/hybrid-search")
@limiter.limit("120/minute")
async def hybrid_search(
    req: HybridSearchRequest,
    request: Request,
    _: str = Depends(require_retrieval_key),
    embedder=Depends(get_embedder),
    engine=Depends(get_engine),
    settings=Depends(get_settings),
) -> HybridSearchResponse:
    """Hybrid search: vector k-NN + full-text keyword fused by RRF."""
    collection_model = COLLECTIONS.get(req.collection)
    if not collection_model:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown collection '{req.collection}'. Choose: {', '.join(COLLECTIONS.keys())}",
        )

    try:
        # Embed the query once
        query_embedding = await embedder.aembed_query(req.query_text)
    except Exception as e:
        log.error(
            "embedding_failed",
            collection=req.collection,
            top_k=req.top_k,
            error=str(e),
        )
        raise HTTPException(status_code=502, detail="Embedding service unavailable") from e

    try:
        async with AsyncSession(engine) as session:
            store = ChunkStore()

            # Recall stage: fetch top-k (or top-50 if reranking is enabled)
            recall_k = 50 if req.enable_reranking else req.top_k
            fused_rows = await store.search_hybrid(
                session,
                query_vector=query_embedding,
                query_text=req.query_text,
                top_k=recall_k,
                distance_threshold=req.distance_threshold,
                sectors=req.sectors,
                project_year_min=req.year_min,
                project_year_max=req.year_max,
                chunk_types=req.chunk_types,
                model=collection_model,
                rrf_k=req.rrf_k,
            )

            # Prepare result objects (common to both branches)
            result_rows = []
            for row in fused_rows:
                chunk = RetrievedChunk(
                    id=row.id,
                    document_id=row.document_id,
                    chunk_type=row.chunk_type,
                    content=row.content,
                    metadata=row.metadata_,
                )
                result_rows.append((chunk, row[-1]))  # (chunk, rrf_score)

            # Rerank stage: if enabled, score with cross-encoder and reorder
            if req.enable_reranking:
                try:
                    reranker = CrossEncoderReranker(req.rerank_model)
                    # Prepare dicts for reranker (content key required)
                    chunk_dicts = [
                        {
                            "id": chunk.id,
                            "document_id": chunk.document_id,
                            "chunk_type": chunk.chunk_type,
                            "content": chunk.content,
                            "metadata": chunk.metadata,
                            "rrf_score": rrf_score,
                        }
                        for chunk, rrf_score in result_rows
                    ]

                    # Rerank and cap at rerank_top_k
                    reranked = await reranker.rerank(
                        req.query_text,
                        chunk_dicts,
                        top_k=req.rerank_top_k,
                    )

                    results = []
                    for chunk_dict, rerank_score in reranked:
                        results.append(
                            HybridSearchResult(
                                chunk=RetrievedChunk(
                                    id=chunk_dict["id"],
                                    document_id=chunk_dict["document_id"],
                                    chunk_type=chunk_dict["chunk_type"],
                                    content=chunk_dict["content"],
                                    metadata=chunk_dict["metadata"],
                                ),
                                rrf_score=chunk_dict["rrf_score"],
                                rerank_score=rerank_score,
                            )
                        )

                    log.info(
                        "hybrid_search_with_reranking_success",
                        collection=req.collection,
                        recall_k=recall_k,
                        rerank_model=req.rerank_model,
                        rerank_top_k=req.rerank_top_k,
                        results_count=len(results),
                        rrf_k=req.rrf_k,
                    )

                    return HybridSearchResponse(
                        results=results,
                        query_text=req.query_text,
                        collection=req.collection,
                        top_k=req.top_k,
                        rrf_k=req.rrf_k,
                        reranked=True,
                        rerank_model=req.rerank_model,
                        rerank_top_k=req.rerank_top_k,
                    )

                except Exception as e:
                    log.error(
                        "reranking_failed",
                        collection=req.collection,
                        rerank_model=req.rerank_model,
                        error=str(e),
                    )
                    raise HTTPException(status_code=502, detail="Reranking service unavailable") from e

            else:
                # No reranking: return RRF-ranked results directly
                results = [
                    HybridSearchResult(
                        chunk=chunk,
                        rrf_score=rrf_score,
                    )
                    for chunk, rrf_score in result_rows
                ]

                log.info(
                    "hybrid_search_success",
                    collection=req.collection,
                    query_len=len(req.query_text),
                    top_k=req.top_k,
                    results_count=len(results),
                    rrf_k=req.rrf_k,
                )

                return HybridSearchResponse(
                    results=results,
                    query_text=req.query_text,
                    collection=req.collection,
                    top_k=req.top_k,
                    rrf_k=req.rrf_k,
                    reranked=False,
                )

    except RetrievalError as e:
        log.error("retrieval_error", collection=req.collection, top_k=req.top_k, error=str(e))
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        log.error(
            "hybrid_search_error",
            collection=req.collection,
            top_k=req.top_k,
            error=str(e),
        )
        raise HTTPException(status_code=500, detail="Internal retrieval error") from e
