"""Cross-encoder wrapper for fine-grained reranking.

Wraps sentence-transformers' CrossEncoder as a configurable, cached singleton.
Used in recall-then-rerank: retriever fetches top-k candidates (recall), then
cross-encoder scores each (query, candidate) pair and reorders (rerank).

Cross-encoders directly score relevance (0–1) for a (query, text) pair,
unlike bi-encoders which embed separately and compute distance. This makes
them more accurate but slower — hence recall-then-rerank: use fast k-NN/BM25
to narrow down, then fine-tune with cross-encoder.

Models are lazy-loaded on first use and cached for performance.
"""

from __future__ import annotations

import structlog
from functools import lru_cache
from typing import Optional

from sentence_transformers import CrossEncoder

log = structlog.get_logger()

# Default cross-encoder: multilingual, ~125M params, fast.
DEFAULT_RERANK_MODEL = "mmarco-mMiniLMv2-L12-H384-v1"


@lru_cache(maxsize=4)
def get_cross_encoder(model_name: str = DEFAULT_RERANK_MODEL) -> CrossEncoder:
    """Lazy-load and cache a CrossEncoder model.

    Models are cached in memory by name. Loading happens once per model per
    process; subsequent calls return the cached instance. Used singleton-like
    within a request context (session/container lifecycle).

    Parameters
    ----------
    model_name : str
        HuggingFace model identifier (e.g., "ms-marco-MiniLM-L-12-v2").

    Returns
    -------
    CrossEncoder
        Loaded model, ready to score (query, text) pairs.
    """
    log.info("loading_cross_encoder", model=model_name)
    encoder = CrossEncoder(model_name, max_length=512, device="cpu")
    log.info("cross_encoder_loaded", model=model_name)
    return encoder


class CrossEncoderReranker:
    """Stateless reranker: scores and reorders chunks by relevance.

    Input: query (str) + chunks (list of dicts with 'content' and optional metadata).
    Output: same chunks, sorted by cross-encoder score (descending).

    Scores are 0–1; higher is more relevant. No filtering by threshold — caller
    decides how many to keep (e.g., top-5, top-10).
    """

    def __init__(self, model_name: str = DEFAULT_RERANK_MODEL):
        """Initialize reranker with a specific cross-encoder model."""
        self.model_name = model_name
        self._encoder = get_cross_encoder(model_name)

    async def rerank(
        self,
        query: str,
        chunks: list[dict],
        top_k: Optional[int] = None,
    ) -> list[tuple[dict, float]]:
        """Score chunks by relevance to query, return top-k sorted by score.

        Parameters
        ----------
        query : str
            User query (short, <500 chars typically).
        chunks : list[dict]
            Candidate chunks. Each dict MUST have a 'content' key with the text.
            Additional keys (id, metadata, etc.) pass through unchanged.
        top_k : int, optional
            Limit results to top-k highest-scoring chunks. If None, return all.

        Returns
        -------
        list[tuple[dict, float]]
            List of (chunk, score) tuples sorted by score DESC.
            Score is normalized to [0, 1] by the cross-encoder.
        """
        if not chunks:
            return []

        # Extract texts for scoring
        texts = [chunk.get("content", "") for chunk in chunks]

        # Score each (query, text) pair
        # CrossEncoder.predict() returns raw logits; we sigmoid them to [0, 1]
        try:
            scores = self._encoder.predict(
                [[query, text] for text in texts],
                convert_to_numpy=True,
            )
        except Exception as e:
            log.error(
                "reranking_error",
                model=self.model_name,
                n_chunks=len(chunks),
                error=str(e),
            )
            raise

        # Pair chunks with scores and sort
        scored = [(chunk, float(score)) for chunk, score in zip(chunks, scores)]
        ranked = sorted(scored, key=lambda x: x[1], reverse=True)

        # Limit to top_k if requested
        if top_k is not None:
            ranked = ranked[:top_k]

        log.info(
            "reranking_complete",
            model=self.model_name,
            n_input=len(chunks),
            n_output=len(ranked),
            top_score=ranked[0][1] if ranked else None,
        )

        return ranked
