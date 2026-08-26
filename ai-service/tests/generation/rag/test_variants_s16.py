"""Session 16 — A/B assignment and the embedding cache. Network-free."""

from __future__ import annotations

import json

from app.generation.rag.embedding.cache import EmbeddingCache, cache_key
from app.generation.rag.variants import assign_variant, plan_for


class FakeRedis:
    """Enough Redis to exercise a read-through cache, and a switch to break it."""

    def __init__(self, *, broken: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.broken = broken
        self.reads = 0

    def get(self, key):
        if self.broken:
            import redis
            raise redis.RedisError("down")
        self.reads += 1
        return self.store.get(key)

    def setex(self, key, ttl, value):
        if self.broken:
            import redis
            raise redis.RedisError("down")
        self.store[key] = value


# --------------------------------------------------------------------------- #
# Assignment
# --------------------------------------------------------------------------- #


def test_the_same_request_always_lands_in_the_same_arm():
    """A retry must not switch arms, or the experiment contaminates itself."""
    rid = "1e6f438a-d668-438b-90e6-585742bff330"
    first = assign_variant(rid, percent_b=50, enabled=True)
    assert all(assign_variant(rid, percent_b=50, enabled=True) == first for _ in range(20))


def test_the_split_is_roughly_the_requested_percentage():
    ids = [f"request-{i}" for i in range(2000)]
    share = sum(1 for i in ids if assign_variant(i, percent_b=25, enabled=True) == "b") / len(ids)
    assert 0.20 < share < 0.30


def test_disabled_means_everyone_gets_a():
    assert all(
        assign_variant(f"r{i}", percent_b=100, enabled=False) == "a" for i in range(50)
    )


def test_zero_and_hundred_are_absolute():
    assert assign_variant("r", percent_b=0, enabled=True) == "a"
    assert assign_variant("r", percent_b=100, enabled=True) == "b"


def test_a_forced_variant_is_labelled_as_forced():
    """So the comparison can exclude it. A demo request is not evidence."""
    plan = plan_for("r", forced="b")
    assert plan.variant == "b"
    assert plan.as_labels() == {"variant": "b", "variant_forced": True}


def test_variant_b_is_the_cost_experiment_and_nothing_else(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    b = plan_for("r", forced="b", settings=settings)
    a = plan_for("r", forced="a", settings=settings)
    # B changes exactly two things: the generation model and the cache.
    assert b.generation_model == settings.AB_VARIANT_B_GENERATION_MODEL
    assert b.embedding_cache is True
    # A leaves the model alone — None means "whatever is configured".
    assert a.generation_model is None


# --------------------------------------------------------------------------- #
# The embedding cache
# --------------------------------------------------------------------------- #


def test_a_hit_returns_the_vector_without_a_second_api_call():
    redis_client = FakeRedis()
    cache = EmbeddingCache(redis_client, ttl=60)
    cache.set("hello", "text-embedding-3-small", None, [0.1, 0.2])

    assert cache.get("hello", "text-embedding-3-small", None) == [0.1, 0.2]
    assert cache.hits == 1


def test_a_different_model_is_a_different_key():
    """Switching embedding models invalidates by construction, not by remembering
    to flush — the alternative is silently mixing two vector spaces."""
    assert cache_key("x", "model-a", None) != cache_key("x", "model-b", None)


def test_the_transcript_text_never_appears_in_the_key():
    """Keys show up in KEYS listings and slow-log lines; client transcripts should
    not."""
    key = cache_key("Acme Corp wants a checkout rebuild", "m", None)
    assert "Acme" not in key


def test_redis_being_down_degrades_to_a_miss_instead_of_an_error():
    """A cache that can take the service down is a liability, not an optimisation."""
    cache = EmbeddingCache(FakeRedis(broken=True), ttl=60)
    assert cache.get("hello", "m", None) is None
    cache.set("hello", "m", None, [0.1])  # must not raise


def test_a_corrupted_entry_is_treated_as_a_miss():
    redis_client = FakeRedis()
    redis_client.store[cache_key("hello", "m", None)] = "not json"
    assert EmbeddingCache(redis_client, ttl=60).get("hello", "m", None) is None


def test_the_stored_value_is_the_vector_itself():
    redis_client = FakeRedis()
    EmbeddingCache(redis_client, ttl=60).set("hello", "m", None, [1.0, 2.0])
    assert json.loads(redis_client.store[cache_key("hello", "m", None)]) == [1.0, 2.0]
