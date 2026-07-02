from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Session 2 fields (kept for backwards compatibility with the live demos) ---
    OPENAI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None
    LLM_PROVIDER: Literal["openai", "anthropic"] = "anthropic"
    LLM_MODEL: str = "claude-haiku-4-5"
    APP_ENV: Literal["development", "staging", "production"] = "development"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "DEBUG"

    # --- Session 3 fields (LiteLLM wrapper, Redis cache, Streamlit transport) ---
    PRIMARY_MODEL: str = "gpt-4o-mini"
    FALLBACK_MODEL: str = "claude-haiku-4-5-20251001"
    LLM_TIMEOUT: int = 30
    LLM_RETRIES: int = 2
    # Catalog of models selectable at runtime via PUT /api/v1/config/models
    # (kept aligned with MODEL_COSTS in app/foundation/llm/wrapper.py). The
    # endpoint filters this list by the API keys actually configured.
    AVAILABLE_MODELS: list[str] = [
        "gpt-4o-mini",
        "gpt-4o",
        "gpt-5",
        "gpt-5-mini",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-5",
    ]

    REDIS_URL: str = "redis://localhost:6379"
    CACHE_TTL: int = 86400

    # --- Session 4 fields (semantic cache) ---
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    SEMANTIC_CACHE_THRESHOLD: float = 0.85
    SEMANTIC_CACHE_TTL: int = 86400
    # When True, the semantic cache LOGS potential hits but does NOT serve them.
    # Used to gather metrics before flipping the cache on in production.
    SEMANTIC_CACHE_LOG_ONLY: bool = False

    ESTIMATOR_API_BASE_URL: str = "http://localhost:8000"

    # --- Session 5 fields (conversational memory + attachments) ---
    # MAX_CONVERSATION_TURNS counts user+assistant pairs. The system prompt is
    # always preserved as an invariant on top of the window.
    MAX_CONVERSATION_TURNS: int = 6
    # Hard cap per extracted attachment (in characters) to protect the context
    # window. Real chunking enters in module 3.
    MAX_ATTACHMENT_CHARS: int = 60_000
    # The metadata extractor runs once per turn; a small/cheap model is enough.
    METADATA_EXTRACTOR_MODEL: str = "gpt-4o-mini"

    # --- Session 5 live: compression + tier + ACB ---
    # Anchor detector: "heuristic" (regex over key phrases) or "llm" (binary
    # classifier via Instructor). Heuristic is the default for cost.
    ANCHOR_DETECTION_MODE: Literal["heuristic", "llm"] = "heuristic"
    # Cheap model used by the cumulative summarizer (history compression).
    COMPRESSION_MODEL: str = "gpt-4o-mini"
    # Conversational prompt version used by ``estimate_conversational``.
    # v2 = pre-live-session baseline. v3 = adds <audience> block driven by tier
    # and an optional <critic_feedback> block consumed by the Boss.
    CONVERSATIONAL_PROMPT_VERSION: str = "v3"
    # Critic model (read-only auditor; cheap is fine).
    CRITIC_MODEL: str = "gpt-4o-mini"
    # Max iterations the Boss can drive (each iteration = 1 actor + 1 critic call).
    # Three is the practical floor: one initial draft + two directed retries.
    # With only two iterations the actor often cannot address all flagged issues
    # in the single available retry, and the loop falls back without converging.
    BOSS_MAX_ITERATIONS: int = 3

    # --- Session 6 fields (data-driven AI: persistence + ingestion + PII) ---
    # Postgres connection string. pgvector/pgvector:pg16 image; the extension
    # is dormant in S06 (no CREATE EXTENSION vector) and only activates in S07.
    DATABASE_URL: str = "postgresql+psycopg://estimator:estimator@localhost:5433/estimator"
    # Where the YAML catalog lives. Resolved relative to the working directory.
    CATALOG_PATH: Path = Path("data/catalog/catalog.yaml")
    # Root where ``CatalogSource.location`` entries are resolved against.
    INGESTION_DATA_ROOT: Path = Path("data/seed")
    # spaCy model loaded by the Presidio AnalyzerEngine. Must be the Spanish
    # one for the live session; ``es_core_news_md`` is the recommended size.
    PRESIDIO_SPACY_MODEL: str = "es_core_news_md"
    # Locale used by Faker to generate consistent pseudonyms per entity_type.
    PSEUDONYM_FAKER_LOCALE: str = "es_ES"
    # HMAC salt. Stored in env so it can be rotated independently of the code.
    PSEUDONYM_HASH_SALT: str = "change-me-in-prod"

    # --- Session 7 live fields (chunking strategies that call external APIs) ---
    # LLM that decomposes a component into atomic propositions (one call per
    # component). A small/cheap model is enough.
    PROPOSITIONAL_CHUNKER_MODEL: str = "gpt-4o-mini"
    # Claude model used by Contextual Retrieval to situate each chunk inside its
    # parent budget. Prompt caching makes the (large) parent document cheap to
    # reuse across the chunks of the same budget.
    CONTEXTUAL_CHUNKER_MODEL: str = "claude-sonnet-4-5"

    # --- Session 9 fields (RAG estimation: transcript → grounded estimate) ---
    # Query understanding distills a transcript into an EstimationQuery; a small
    # model is enough. Generation reasons over retrieved budgets, so it uses the
    # strongest model with medium reasoning effort. Both go through LLMWrapper.
    REFORMULATION_MODEL: str = "gpt-5-mini"
    GENERATION_MODEL: str = "gpt-5"
    # "high" drives a deeper, more consistent module→task decomposition (the S09
    # article used "medium"; we raise it for the granular modular breakdown).
    GENERATION_REASONING_EFFORT: Literal["minimal", "low", "medium", "high"] = "high"
    # Token ceiling (reasoning + output) for the RAG structured calls. gpt-5 is a
    # reasoning model: its reasoning tokens count against this budget, so the
    # 4000 wrapper default leaves nothing for the JSON and the call truncates
    # (finish_reason='length'). Generous headroom so high-effort reasoning can
    # finish AND emit the larger nested (modules→tasks) Estimate. It is a CAP,
    # not a target — the model only spends what it needs, so a high value adds no
    # latency on its own.
    GENERATION_MAX_TOKENS: int = 64000
    # Retrieval knobs (locked defaults from the Session 9 articles).
    RETRIEVAL_TOP_K: int = 10
    RETRIEVAL_DISTANCE_THRESHOLD: float = 0.6
    # Token budget for the assembled <source> context block (tiktoken cl100k_base).
    MAX_CONTEXT_TOKENS: int = 16384
    # Idempotency cache for POST /v1/estimate/from-transcript (seconds; 24h).
    IDEMPOTENCY_TTL: int = 86400
    # API keys for the two Session 9 routers. None disables the router (401 on
    # every request) — set them in .env to enable the endpoints.
    RETRIEVAL_API_KEY: str | None = None
    ESTIMATE_API_KEY: str | None = None

    # --- Session 10 fields (hybrid search + cross-encoder reranking) ---
    # Default retrieval mode. "vector" reproduces the Session 9 baseline; "hybrid"
    # fuses the dense and lexical (full-text) branches with RRF. Switchable per
    # request (RetrievalRequest.search_mode) and at runtime (RuntimeRetrievalConfig).
    RETRIEVAL_SEARCH_MODE: Literal["vector", "hybrid"] = "vector"
    # Whether the cross-encoder reranks by default. Off keeps the baseline cheap;
    # the recall-then-rerank path turns on per request / at runtime.
    RERANKER_ENABLED: bool = False
    # Multilingual cross-encoder (ES+EN), small enough for CPU at teaching latency.
    RERANKER_MODEL: str = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    # Recall width before reranking/fusion (recall-then-rerank): retrieve this many
    # candidates cheaply, then the cross-encoder rescores them down to RERANK_TOP_N.
    RETRIEVAL_RECALL_TOP_K: int = 50
    RERANK_TOP_N: int = 5
    # RRF smoothing constant (Cormack et al. default). Larger = a document must
    # rank well in BOTH branches to win; smaller = a single #1 can dominate.
    RRF_K: int = 60

    # --- Session 10 live fields (advanced retrieval: multi-index pipeline) ------
    # Each advanced-retrieval stage is independently switchable so it can be
    # measured in isolation (the full pipeline is the MAX path, not the only one).
    # These are the .env defaults; routing/transform/decay also flip at runtime
    # (RuntimeRetrievalConfig → Ajustes UI). Search mode + reranking reuse the
    # existing RETRIEVAL_SEARCH_MODE / RERANKER_ENABLED toggles above.
    RETRIEVAL_ROUTING_ENABLED: bool = True
    QUERY_TRANSFORM_ENABLED: bool = True
    # Soft re-weight; off by default — turn on only with evidence (Article 6's
    # warning against magic-number boosts).
    TEMPORAL_DECAY_ENABLED: bool = False
    # Small, fast models for the router classifier and the query transformer
    # (both in AVAILABLE_MODELS, so switchable in the Ajustes tab). Non-reasoning
    # models on purpose: cheap and no reasoning-token budget to starve the JSON.
    ROUTER_MODEL: str = "gpt-4o-mini"
    QUERY_TRANSFORM_MODEL: str = "gpt-4o-mini"
    # Exponential half-life for temporal decay (weight = 0.5 ** (age/half_life)).
    # ≈2.5 years: budgets age slowly, so recency only breaks ties.
    TEMPORAL_DECAY_HALF_LIFE_DAYS: int = 900
    # Caps for the query transformer (sub-queries) and the router (targets).
    QUERY_MAX_SUBQUERIES: int = 4
    ROUTER_MAX_TARGETS: int = 3

    # --- Session 10 live fields (per-task hours estimation) ---------------------
    # The structure-only generation leaves tasks without hours; each task is then
    # matched against the historical task corpus (chunk_type 'historical_task') and
    # the hours come from a weighted consensus of the nearest neighbours. These two
    # knobs change mid-session (calibrating the red threshold against the corpus),
    # so they flip at runtime via RuntimeRetrievalConfig → Ajustes UI.
    TASK_HOURS_TOP_K: int = 5
    # Cosine-distance floor: a task whose nearest historical task is farther than
    # this gets NO hours (red flag in the UI) instead of a low-confidence guess.
    TASK_HOURS_DISTANCE_THRESHOLD: float = 0.45

    # --- Session 11 live fields (generation quality: hallucination gate) --------
    # A SEMANTIC layer on top of the referential citation check (verify_citations):
    # verify_citations proves every cited chunk_id was retrieved; the gate proves
    # the number is ENTAILED by that chunk. A deterministic numeric anchor + a
    # strict LLM judge grade each grounded line grounded / insufficient / degraded.
    # Switchable at runtime (RuntimeRetrievalConfig → Ajustes UI).
    HALLUCINATION_GATE_ENABLED: bool = True
    # The strict judge that checks a line's cited evidence entails its number. A
    # cheap model is enough; in AVAILABLE_MODELS, so switchable in the Ajustes tab.
    HALLUCINATION_JUDGE_MODEL: str = "gpt-5-mini"
    # Relative tolerance for the numeric anchor: a grounded line whose hours deviate
    # from the historical anchor by more than this fraction is degraded (0.5 = ±50%).
    HALLUCINATION_NUMERIC_TOLERANCE: float = 0.5

    # --- Session 11 live fields (augmentation + synthesis) ----------------------
    # Input-quality layer applied to the retrieved chunks BEFORE generation:
    # compress each source to its key points and reorder with edge-loading
    # (most-relevant first AND last) against lost-in-the-middle. Both switchable
    # at runtime; the reorder is a pure, free transform, the compression optional.
    AUGMENTATION_ENABLED: bool = True
    AUGMENTATION_COMPRESS: bool = True
    AUGMENTATION_REORDER: bool = True
    # Cheap model for the optional LLM compression (extractive compression needs
    # none). In AVAILABLE_MODELS.
    AUGMENTATION_MODEL: str = "gpt-5-mini"
    # Two-stage synthesis of the per-task hours: a deterministic anchor + model
    # judgement. When the historical sources disagree beyond this dispersion
    # (coefficient of variation, e.g. one says 40h and another 90h), emit an hour
    # RANGE with a reason instead of a single point. Switchable at runtime.
    SYNTHESIS_ENABLED: bool = True
    SYNTHESIS_CONTRADICTION_THRESHOLD: float = 0.35

    @model_validator(mode="after")
    def validate_at_least_one_api_key(self) -> "Settings":
        """LiteLLM may try either provider via fallback, so we require at least one key."""
        if not self.OPENAI_API_KEY and not self.ANTHROPIC_API_KEY:
            raise ValueError("At least one of OPENAI_API_KEY or ANTHROPIC_API_KEY must be set")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings (singleton)."""
    return Settings()
