"""Session 16 — cache for query embeddings.

The cheapest saving in the whole system, and until now it was not taken.

An embedding is a pure function of (text, model, dimensions): the same input
always produces the same vector, forever. Paying for one twice buys nothing.
That is not true of a generation — which is why the CAG caches from Session 4
need a similarity threshold and a TTL and a whole discussion about when a
"close enough" answer is close enough. Here there is no such question. Same key,
same answer, no judgement involved.

WHAT IT DOES NOT CACHE. Ingestion (``embed_many``). Corpus embedding happens once
per document by design, so a cache there would grow without bound to serve hits
that never come. This caches the QUERY side, where the same question really does
get asked again — the same transcript re-submitted, an idempotent retry, a golden
set run twice, an evaluation sweep across variants.

INVALIDATION. The model name is part of the key, so switching embedding models
invalidates the cache by construction rather than by remembering to flush it. The
TTL is a safety valve for storage, not a correctness mechanism: a cached vector
never goes stale, it only stops being worth its space.

Redis being down degrades to a plain call. A cache that can take the service down
is a liability, not an optimisation.
"""

from __future__ import annotations

import hashlib
import json

import redis
import structlog

log = structlog.get_logger()

KEY_PREFIX = "estimator:embcache"

__all__ = ["EmbeddingCache", "cache_key"]


def cache_key(text: str, model: str, dimensions: int | None) -> str:
    """Stable key for one embedding request.

    The text is hashed rather than stored: transcripts are large, and they carry
    client information that has no business sitting in a cache key where it shows
    up in every ``KEYS`` listing and every Redis slow-log line.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{KEY_PREFIX}:{model}:{dimensions or 'native'}:{digest}"


class EmbeddingCache:
    """Read-through cache for single query embeddings."""

    def __init__(self, redis_client: redis.Redis | None, ttl: int) -> None:
        self._redis = redis_client
        self._ttl = ttl
        self.hits = 0
        self.misses = 0

    @classmethod
    def from_url(cls, url: str, ttl: int) -> "EmbeddingCache":
        try:
            return cls(redis.from_url(url, decode_responses=True), ttl)
        except Exception as exc:  # noqa: BLE001 — never fail on cache construction
            log.warning("embedding_cache_unavailable", error=str(exc)[:200])
            return cls(None, ttl)

    def get(self, text: str, model: str, dimensions: int | None) -> list[float] | None:
        if self._redis is None:
            return None
        try:
            raw = self._redis.get(cache_key(text, model, dimensions))
        except redis.RedisError as exc:
            log.warning("embedding_cache_read_failed", error=str(exc)[:200])
            return None
        if raw is None:
            self.misses += 1
            return None
        try:
            vector = json.loads(raw)
        except json.JSONDecodeError:
            self.misses += 1
            return None
        self.hits += 1
        log.info("embedding_cache_hit", model=model)
        return vector

    def set(self, text: str, model: str, dimensions: int | None, vector: list[float]) -> None:
        if self._redis is None:
            return
        try:
            self._redis.setex(
                cache_key(text, model, dimensions), self._ttl, json.dumps(vector)
            )
        except redis.RedisError as exc:
            log.warning("embedding_cache_write_failed", error=str(exc)[:200])
