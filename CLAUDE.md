# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

Two-project monorepo for the Master en AI Engineering programme:

- `estimator/` — FastAPI service. The AI side: prompts, LLM calls, structured output, guardrails, semantic cache. All AI logic lives here; the rest of the programme evolves this codebase module by module.
- `estimator-web/` — Rails 8 frontend + business backend (Postgres + Tailwind + Hotwire). Reference implementation of the cliente; each student is free to use a different stack. The live sessions invoke `estimator` directly via httpie/curl (stack-agnostic).

A root-level `docker-compose.yml` orchestrates both via the `include:` directive (Compose v2.20+). Running `docker compose up` from the repo root brings up all 5 services (`estimator`, `redis`, `estimator-postgres`, `estimator-web`, `postgres`) on a shared network so Rails can call the FastAPI estimator at `http://estimator:8000`. Note there are **two** Postgres instances on purpose: `estimator-postgres` (pgvector image, host port 5433, used by the FastAPI service) and `postgres` (alpine, host port 5432, used by Rails). `include:` forbids two included files from declaring the same resource name, so the FastAPI one is named `estimator-postgres` (not plain `postgres`) and its volume is `estimator_postgres_data` — renaming either back to `postgres`/`postgres_data` re-introduces the `services.postgres conflicts with imported resource` error at the root.

**Trap to be aware of**: launching from the root vs from a subdirectory creates *different* Compose projects, which means the named volumes (`postgres_data`, `estimator_postgres_data`, `bundle_cache`, `redis_data`) are not shared between the two modes. Pick a mode per workflow and stay with it.

Session guides for the instructor live in `guides/` (git-ignored). `guides/session-4-live-guide.md` is the most recent.

## Common commands (estimator)

Dependency / runtime management uses **uv** (Astral) and Python 3.11.

```bash
cd estimator

# Run the API locally with hot reload
uv run uvicorn app.main:app --reload

# Tests
uv run pytest -v
uv run pytest tests/test_schemas.py::test_phases_sum_must_equal_total_cost -v

# Lint
uv run ruff check .
uv run ruff format .

# Docker (recommended dev path — bind-mounts app/ and tests/ for live reload)
docker compose up --build
```

Service listens on `http://localhost:8000`; `/docs` (Swagger) and `/redoc` are enabled. Health probe at `GET /health`. Main API endpoints: `POST /api/v1/estimate` (S04 CAG estimate) and, from S09, `POST /v1/retrieval/search` + `POST /v1/estimate/from-transcript` (RAG retrieval + grounded estimate; see the Session 9 design point below).

## Architecture (layered: foundation / domain / generation / api)

The estimator is organized around the **three AI architectures it stacks** — CAG (caches), RAG
(retrieval) and Agentic (Actor-Critic-Boss) — which **compose only through a single conductor**.
Full contract in **`estimator/ARCHITECTURE.md`** — respect it for all new session code.

`app/` layers (each may import only from layers above it):

```
app/
├── config.py · dependencies.py · main.py   # composition root, above the layers
├── foundation/   llm · prompts · guardrails · attachments · persistence  (no AI-arch opinion)
├── domain/       schemas/ (the contract) + estimation_service.py (the conductor)
├── generation/   cag/ · rag/ · agentic/ · conversation/   (the 3 architectures + substrate)
├── ingestion/    offline batch pipeline that feeds RAG
└── api/          thin routers (transport)
```

Five-layer request pipeline. Free-text in, validated structured JSON out:

```
POST /api/v1/estimate
  └→ app/api/estimations.py    (thin HTTP layer, error mapping)
       └→ app/domain/estimation_service.py::EstimationService.estimate()
            1. app/foundation/guardrails/input.py::check_input()      (moderation + injection + PII)
            2. app/generation/cag/exact.py::EstimationCache.get()     (exact-match SHA-256)
            3. app/generation/cag/semantic.py::EstimationSemanticCache.lookup()
                                                                (redisvl vector similarity)
            4. app/foundation/prompts/loader.py::render_estimation_prompt()  (Jinja2 versioned)
            5. app/foundation/llm/wrapper.py::complete_structured()
                                                                (Instructor + Pydantic validators
                                                                 with automatic re-prompt)
            6. app/foundation/guardrails/output.py::enforce_scope_response() (filter policy)
            7. cache.set() + semantic_cache.store()
            8. return EstimationResponse(result, prompt_version, cached)
```

**Layering rules** (see `estimator/ARCHITECTURE.md` for the full table):
- `foundation/` imports only `config`. `domain/schemas` imports `foundation`. `generation/<x>`
  imports `foundation` + `domain/schemas` but **never another `generation` sibling** (the one
  exception: `agentic` may import `conversation`).
- The `generation` siblings (cag/rag/agentic) meet **only** inside the conductor
  (`domain/estimation_service.py`). New cross-layer composition goes there, never in a router
  and never via a sibling import.
- `api/` is transport only (error mapping); `dependencies.py` is the composition root that wires
  every singleton and is allowed to import anything.

Key design points future changes should respect:

- **The router has no business logic.** It only catches three exceptions and turns them into HTTP statuses: `InputGuardrailViolation` → 400, anything else from the pipeline (including `instructor.exceptions.InstructorRetryException`) → 502, plus Pydantic 422 from `EstimationRequest` validation. Add new policies inside `EstimationService.estimate()`, not in the router.
- **Schema is the contract.** `EstimationResult` (in `app/domain/schemas/estimation.py`) is what Instructor enforces against the LLM. The two `model_validator`s (`phases_sum_matches_total`, `low_confidence_requires_out_of_scope_prefix`) are the business rules — when they raise, Instructor re-prompts the LLM up to `max_retries=6` times.
- **Field order matters with Instructor.** `phases` is declared BEFORE `total_cost_eur` / `total_duration_weeks` on purpose: the LLM emits phases first (autoregressive) and then only needs to sum, instead of picking a round total and back-fitting phases. With smaller models like `gpt-4o-mini` this is the difference between consistent success and arithmetic failures.
- **Two caches in series.** Both live in the CAG layer (`app/generation/cag/`). The exact-match cache (`app/generation/cag/exact.py`) keys on SHA-256 of the typed request + prompt_version + model. The semantic cache (`app/generation/cag/semantic.py`) layers on top: same bucket (`prompt_version:project_type:detail_level:output_format`) + cosine similarity ≥ `SEMANTIC_CACHE_THRESHOLD` (default 0.85). The semantic cache requires Redis Stack (`redis/redis-stack:7.4.0-v0`), not vanilla Redis — RediSearch is mandatory for vector queries.
- **Guardrails are policies, not features.** `check_input` uses `exception` policy (raise on violation). `enforce_scope_response` uses `filter` (rewrite the summary). The schema validators use `re-prompt` (Instructor handles it). The split is documented in the live-session guide.
- **Settings are a cached singleton** via `app/config.py::get_settings` (`@lru_cache`). Any change to `.env` requires recreating the container (`docker compose up -d --force-recreate`); a `--reload` is not enough. **Exception: the LLM model knobs** (`PRIMARY_MODEL`, `FALLBACK_MODEL`, `CRITIC_MODEL`, metadata/compression/chunker models) can be overridden at runtime via `PUT /api/v1/config/models` (Redis-backed `app/foundation/llm/runtime_config.py`, surfaced in the Rails "Ajustes" tab) — overrides survive `--reload` and restarts, and both caches partition by model.
- **Logging** is `structlog`. JSON in `production`, console in dev. Use `structlog.get_logger()` rather than stdlib `logging`.
- **The LLM wrapper bypasses the Router for streaming and for structured calls** (see `_dispatch`). LiteLLM's Router does round-robin between deployments, which would non-deterministically route to a fallback that may be unreachable. For deterministic behaviour `complete_structured` always uses the primary model directly.
- **Session 9 closes the transcript → estimate loop (RAG generation).** A second, RAG-native estimate path lives entirely in `app/generation/rag/` and is exposed by two independently-secured routers in `app/api/routers/`:
  - `POST /v1/retrieval/search` (auth `RETRIEVAL_API_KEY`, 120/min) — metadata-filtered k-NN with a relevance threshold + soft-fail. It supersedes the unauthenticated Session 8 `POST /search`, which stays only for backwards compatibility (Chunking Lab / S08 demos).
  - `POST /v1/estimate/from-transcript` (auth `ESTIMATE_API_KEY`, 10/min, idempotent on `idempotency_key`) — runs `estimate_from_transcript`: `reformulate_query` → `compose_search_text` + embed → `search_chunks` (soft-fail short-circuits to `confidence="insufficient"`) → `truncate_to_token_budget` → `build_context_block` (XML `<source>` delimiters) → `generate_estimate` → `validate_citations` (one corrective retry on fabricated ids) → coherence check.
  This path **reuses `LLMWrapper`** (Instructor + LiteLLM) for both reformulation (`REFORMULATION_MODEL`, default `gpt-5-mini`) and generation (`GENERATION_MODEL`, default `gpt-5`, `reasoning_effort="high"`, `max_tokens=GENERATION_MAX_TOKENS` default 64000 — reasoning tokens count against the budget) — NOT the raw OpenAI Responses API. It emits the hours-based `Estimate` schema: a nested `modules` → `tasks` breakdown (`WorkModule`/`TaskItem`, each task with `engineer_days` + `sources`) plus `total_engineer_days` and mandatory `SourceCitation`s + `Assumption`s — distinct from and coexisting with the Session 4 euro/weeks `EstimationResult`. The engineer-day numbers are **LLM-inferred**, grounded in the historical `estimated_hours` the model reads from the retrieved `<source>` chunk text (the retriever does no numeric aggregation). To ground the *task-granular* breakdown there is an optional task-level corpus: `scripts/build_task_corpus.py` deterministically synthesises projects decomposed into modules→tasks (each task = a `BudgetComponent` carrying the new optional `module` field, surfaced by the structural chunker), writes `data/task_corpus.json`, and `--ingest`s it via `/embeddings/ingest` tagged `document_type='historical_task_breakdown'` / `chunk_type='historical_task'` (filterable; `IngestRequest.chunk_type` defaults to `budget_component`, so S08 ingest is unchanged). The default corpus is **60 projects / ~1.5k tasks** across **eight sectors** (`finance`, `ecommerce`, `healthcare`, `industrial`, `logistics`, `education`, `media`, `government` — the `Sector` literal in `app/generation/rag/schemas.py`) with a broad module catalog, so the Session 10 per-task hours search (`POST /v1/estimate/tasks/hours`, weighted-consensus over the nearest historical tasks) has many analogs to match; `--count`/`--seed` tune it. It coexists with the base corpus; wipe with `DELETE FROM documents WHERE document_type='historical_task_breakdown'`. A teaching-only set of per-stage endpoints (`POST /v1/estimate/stages/{reformulate,retrieve,assemble,generate,structure}`, `app/api/routers/estimate_stages.py`) exposes each pipeline step, reusing the same pure functions. **Session 10 reshaped the Rails wizard flow** (`estimator-web`, `Rag::EstimationRun`): it no longer retrieves/augments before generation — the structure is a FREE LLM decomposition of the reformulated brief via `POST /v1/estimate/stages/structure` (`generate_structure` + `build_structure_system_prompt`, no `<sources>`, no citations, `engineer_days` null), grounding the *structure* in retrieved budgets impoverished the tree. Retrieval re-enters **per task** in `POST /v1/estimate/tasks/hours` (`app/generation/rag/task_hours.py`): each reviewed task is searched via `retrieve()` (hybrid + cross-encoder reranking, per the runtime `RERANKER_ENABLED`) filtered to `chunk_type='historical_task'`, and the hours come from a distance-weighted **consensus** of the nearest neighbours with a reliability score (no match under `TASK_HOURS_DISTANCE_THRESHOLD` → no hours, flagged red). The wizard steps are now `transcript → reformulation → generation(structure) → review → hours → verification`; `from-transcript` (grounded, hours inline) and `/stages/generate` stay as the Session 9 comparison path. Cross-cutting: per-API-key rate limiting (`app/api/rate_limiting.py`, slowapi), constant-time key checks (`app/api/security.py`, `secrets.compare_digest`), idempotency store (`app/generation/rag/idempotency.py`, Redis or in-process fallback), and an `X-Request-ID` correlation header set by middleware in `app/main.py` (per-stage logs via `log_stage`).

- **Session 10 adds advanced retrieval (multi-index, routing, expansion, decay).** The corpus is partitioned into **three chunk tables** — `budget_chunks` (the Session 8 `chunks` table, renamed in migration `0004_session10_multi_index`), `transcript_chunks` and `technical_doc_chunks` — sharing the `_ChunkColumns` ORM mixin but each with its own JSONB metadata schema (Article 5 "Opción B": schemas that diverge → separate tables). `ChunkRow` stays an alias of `BudgetChunkRow` so Session 8/9 imports are unaffected; `ChunkStore` search/persist methods take a `model=` (default `BudgetChunkRow`). The whole advanced layer lives under `app/generation/rag/retrieval/`: `collections.py` (the `Collection` StrEnum + registry: per-collection model, date accessor, rule patterns, hard-filter clauses), `router.py` (cascade routing: explicit collection → deterministic vocab rules → LLM classifier with structured 1–3 targets + reason → fallback-to-all), `query_transform.py` (expansion vs decomposition chosen by a length/connectors heuristic, ≤4 sub-queries via structured output), `fusion.py` (`reciprocal_rank_fusion` for expansion consensus + `round_robin_merge` for decomposition coverage, deduped by `(collection, id)` since ids only collide-free within a table), `temporal.py` (exponential decay, applied LAST on a non-negative base — reranker logits are sigmoid'd first), and `advanced_pipeline.py` (the conductor: query transform → routing → hard filters → hybrid search → differentiated fusion → rerank → temporal decay → top-k, every stage gated by a `StageConfig`). It is exposed by `POST /v1/retrieval/advanced-search` (`app/api/routers/retrieval_advanced.py`, auth `RETRIEVAL_API_KEY`, 120/min) whose response surfaces the routing decision, technique, sub-queries and per-collection cardinality. **The Session 9 `POST /v1/retrieval/search` and the estimate path are untouched** (`retrieve()` keeps its single-collection contract; it just gained a `collection=` default of budget). Stage toggles flip at runtime via `RuntimeRetrievalConfig` → `PUT /api/v1/config/retrieval`. New sample collections: `data/transcripts_sample.json` + `data/technical_docs_sample.json`, seeded by `scripts/build_multi_index_corpus.py` (run inside the container); the harness `scripts/eval_retrieval_s10.py` runs named `StageConfig`s (data, not code branches) against the extended multi-collection golden set. **tsvector config stays `english`** everywhere (the shipped corpus is English; see migration 0003) and **all advanced LLM calls reuse `LLMWrapper`** (Instructor), NOT the raw Responses API — flagged because the articles taught `responses.parse`.

- **Session 12 adds a hand-written agentic layer (manual Responses API loop).** Where the S9–S11 estimate path is a *fixed* pipeline (reformulate → retrieve → generate), the agent *decides* at each step how many budget searches to run and in what order — the right shape for a transcript that mixes several unrelated components (e.g. business backend + ERP integration + mobile app). It lives under `app/generation/agentic/` alongside the untouched S4 ACB files (`boss.py`/`critic.py`): `agent_schemas.py` (tool arg models + trace models `AgentStep`/`AgentTrace` with a `render()` for the `STEP N` console format + the LIGHT result `AgentEstimate`, deliberately distinct from the heavy RAG `Estimate`), `agent_tools.py` (three **flat** Responses tool schemas with `strict:true` — `search_budgets`, `calculate_estimate`, `validate_estimate` — plus impls and an async `dispatch_tool`), and `agent_loop.py` (`run_estimation_agent`: the manual reason→act→observe loop). **This is the one deliberate exception to the "everything goes through `LLMWrapper`" rule** — the agent drives the raw OpenAI **Responses API** (`client.responses.create`/`.parse`) by hand, because seeing the loop is the whole point of the exercise (do NOT "fix" it to use `LLMWrapper`). Loop mechanics: **stateful chaining** (`store=True` + `previous_response_id` + only the new `function_call_output` items each turn, so the server retains reasoning-item ordering — avoids the gpt-5 ordering pitfalls); `reasoning={"effort":…,"summary":"auto"}` surfaces reasoning summaries for the trace; a `max_iterations` safeguard bounds the natural stop (a turn with no `function_call`); tool errors are returned as the output string so the model self-corrects instead of crashing the loop; a terminal `responses.parse(text_format=AgentEstimate)` yields the validated result. `search_budgets` **wraps the real `retrieve()`** (budget collection, `chunk_type='historical_task'`) via an **injectable backend** — the student stub `exercises/session-12/reference_retrieval.py` swaps in for offline loop debugging. A new async client factory `dependencies.get_async_openai_client()` (mirrors `get_openai_client()`) backs the loop. There is **no HTTP endpoint or Rails UI this session** (pre-exercise scope; the live session adds those). Run via `scripts/run_agent_s12.py` (CLI flags `--model`/`--effort`/`--max-iterations`/`--stub`/`--out`). The student kit + reference-solution pointers live in `estimator/exercises/session-12/` (two transcripts, the stub, a `calculate_estimate` skeleton, README); the committed deliverable trace is `exercises/session-12/example_trace_complex.txt`. Tests are network-free (`tests/generation/agentic/`, a scripted fake `AsyncOpenAI`).

- **Session 13 re-expresses the estimation flow as an explicit LangGraph `StateGraph`.** Where S12 drove the flow with a hand-written reason→act→observe loop, S13 makes it a graph: five sequential nodes over a typed shared state, with Postgres persistence and Logfire observability. The **external contract is unchanged** — transcript in, structured estimate + `status` out. It lives under `app/domain/graph/` (a **conductor**, like `estimation_service.py`: it composes `generation/rag` retrieval + `foundation/llm` generation, so it goes in `domain/`, not in a `generation` sibling — see `ARCHITECTURE.md` §4). Pieces: `state.py` (the `EstimationState` `TypedDict` with **two accumulator reducers** — `budget_matches` and `errors` — via `Annotated[..., operator.add]`; note it uses `typing_extensions.TypedDict`, required by Pydantic on Python <3.12); `nodes.py` (the five pure `state → partial update` async nodes — `extract_requirements` → `classify_components` → `search_budgets` → `generate_estimate` → `validate_and_consolidate` — each wrapped in `logfire.span("node: …")`; the LLM nodes reuse `LLMWrapper.complete_structured`, `search_budgets` reuses the real S9/S10 `make_retrieval_backend()` over `chunk_type='historical_task'` **one component at a time** — sequential on purpose, the live session parallelises it with the Send API; nodes self-wire deps via local `from app.dependencies import ...`); `build.py` (`build_graph(checkpointer)` — sequential edges + a **Level-3 conditional edge** `route_on_status` from `validate_and_consolidate`, `"validated"`/`"needs_review"` both → `END`); `checkpointer.py` (`AsyncPostgresSaver` over the project Postgres — `saver_conninfo` strips the SQLAlchemy `+psycopg`/`+asyncpg` driver token from `DATABASE_URL` to a plain libpq DSN, since the saver is **psycopg3, not asyncpg**; its tables coexist with pgvector; `open_checkpointer()` calls `setup()` once); `observability.py` (`configure_logfire(app)` — `instrument_fastapi` + `instrument_httpx`, **no-op without `LOGFIRE_TOKEN`** via `send_to_logfire="if-token-present"`, never breaks startup). The graph is built with its checkpointer in `main.py`'s `lifespan` (guarded — a Postgres failure leaves `app.state.graph = None` and the endpoint 503s without taking down other routers) and exposed by `POST /v1/estimate/graph` (`app/api/routers/estimate_graph.py`, auth reuses `ESTIMATE_API_KEY`, 10/min, `thread_id = estimation_id`). New deps: `langgraph`, `langgraph-checkpoint-postgres`, `logfire[fastapi,httpx]`. Config knobs: `GRAPH_EXTRACTION_MODEL` (default `gpt-4o-mini`) and `GRAPH_GENERATION_MODEL` (default **`gpt-4o`** — `gpt-4o-mini` unreliably leaves the numeric `engineer_days` field null) + `LOGFIRE_SERVICE_NAME`. Run via `scripts/run_graph_s13.py` (`--memory` = `MemorySaver` instead of Postgres; `--stub` = the offline S12 reference retrieval; `--out`). Student kit in `estimator/exercises/session-13/` (README, the transcript, `example_run_complex.txt`). Tests are network-free (`tests/domain/graph/`, `MemorySaver` + a scripted fake `LLMWrapper` + fake retrieval backend).

  **The live session grows that 5-node pipeline into a MULTI-AGENT flow** (branch `session_13_live`; the 5-node graph above is the pre-exercise "before"). The nodes become a pipeline of specialised agents under `app/domain/graph/agents/` with **two explicit handovers** (`Command(goto=…, update=…)`) and **two human gates** (`interrupt()` / resumed with `Command(resume=…)`): `classifier_agent` (complexity + reformulation, gpt-4o-mini) **─Command─▶** `structure_agent` (modules→tasks, reuses S12 `run_structure_agent`/gpt-5) **─▶** `human_gate_structure` (interrupt #1) **─Send fan-out, one branch per task─▶** `estimate_task_hours` ×N (deterministic per-task hours, reuses S10 `estimate_one`) **─join─▶** `recover_and_handover` (agentic recovery of doubtful tasks via S12 `run_task_hours_recovery_agent`, then builds the estimate) **─Command─▶** `analysis_agent` (reliability report, gpt-4o) **─▶** `human_gate_analysis` (interrupt #2) **─conditional─▶** `proposal_agent` (bonus commercial proposal, gpt-4o) | `END`. `state.py` gains the flow fields + a **keyed reducer `merge_task_hours`** (dedupe by `(module, task)`, NOT `operator.add` — idempotent across a resume that re-enters the fan-out; the gate nodes call `interrupt()` BEFORE any reducer write, since resume re-executes the whole node). `build.py` is rewired accordingly (`fan_out_hours` emits `Send`s, `route_after_gate2` routes to proposal/END). `checkpointer.py` now backs `AsyncPostgresSaver` with an `AsyncConnectionPool` (reconnects across the day-long human pauses). New node schemas `ComplexityClassification` / `ReliabilityReport` / `CommercialProposal` (`graph/schemas.py`); new HTTP contract `GraphResumeRequest` / `GraphRunState` / `PendingGate` (`domain/schemas/graph_estimation.py`). The router keeps `POST /v1/estimate/graph` (START → pauses at gate 1) and adds `POST /v1/estimate/graph/{estimation_id}/resume` (feeds a `Command(resume=…)`; 409 if nothing pending) + `GET …/state` (read the pending gate via `aget_state`). New config knobs `GRAPH_CLASSIFIER_MODEL` (gpt-4o-mini), `GRAPH_ANALYSIS_MODEL`/`GRAPH_PROPOSAL_MODEL` (gpt-4o), `GRAPH_PROPOSAL_ENABLED`, `GRAPH_STRUCTURE_EFFORT_BY_COMPLEXITY`; reuses `AGENT_MODEL`/`AGENT_MAX_ITERATIONS`/`AGENT_SEARCH_*`/`TASK_HOURS_*`. `scripts/run_graph_s13.py` drives the full flow auto-approving both gates. `tests/domain/graph/` cover the handovers, the fan-out keyed reducer, both interrupt gates + resume, and idempotency. **On the cliente (`estimator-web`) the live session adds a graph-driven wizard**: `RagEstimateClient#graph_start/#graph_resume/#graph_state`, `Rag::GraphEstimationRun` (table `graph_estimation_runs`) + `Rag::GraphEstimationRunsController` (START at `create`, RESUME at `resume_structure`/`resume_final`, reusing the module editor + `guard_rag_errors`), routes `resources :graph_estimation_runs`, and views under `app/views/rag/graph_estimation_runs/`. The `interrupt()` gates are the platform's existing human review screens; the service IA only exposes START + the resume points (the pattern is stack-agnostic — any HTTP client can drive resume). The guide is `guides/session-13-live-guide.md`.

## Configuration

`.env` (copied from `.env.example`) drives everything via `pydantic-settings`.

Session 2/3 vars:
- `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` — at least one required.
- `PRIMARY_MODEL` / `FALLBACK_MODEL` — LiteLLM Router config.
- `LLM_TIMEOUT` / `LLM_RETRIES` — per LLM call.
- `REDIS_URL` — points to the Redis Stack container in compose.

Session 4 vars:
- `EMBEDDING_MODEL` — defaults to `text-embedding-3-small`.
- `SEMANTIC_CACHE_THRESHOLD` — cosine similarity threshold (0..1). 0.85 default = the typical range mentioned in the live guide. Lower = more hits, more false positives.
- `SEMANTIC_CACHE_TTL` — seconds (24h default).
- `SEMANTIC_CACHE_LOG_ONLY` — when `true`, the cache logs would-be hits but never serves them. Use it to calibrate the threshold against real traffic before flipping on.

Session 9 vars (RAG estimation):
- `RETRIEVAL_API_KEY` / `ESTIMATE_API_KEY` — independent keys for the two routers (header `X-API-Key`). Blank disables the router (401 on every request).
- `REFORMULATION_MODEL` / `GENERATION_MODEL` / `GENERATION_REASONING_EFFORT` — default `gpt-5-mini` / `gpt-5` / `medium`. In `AVAILABLE_MODELS`, so switchable at runtime via the Ajustes tab.
- `RETRIEVAL_TOP_K` / `RETRIEVAL_DISTANCE_THRESHOLD` — locked defaults `10` / `0.6` (cosine distance).
- `MAX_CONTEXT_TOKENS` — token budget for the assembled `<source>` block (tiktoken `cl100k_base`; default 16384).
- `IDEMPOTENCY_TTL` — seconds (24h). Idempotency store uses `REDIS_URL` when reachable, else an in-process dict.

Session 10 vars (advanced retrieval):
- `RETRIEVAL_ROUTING_ENABLED` / `QUERY_TRANSFORM_ENABLED` / `TEMPORAL_DECAY_ENABLED` — per-stage toggles (defaults `true`/`true`/`false`). Also flip at runtime via `PUT /api/v1/config/retrieval` (Redis-backed `RuntimeRetrievalConfig`).
- `ROUTER_MODEL` / `QUERY_TRANSFORM_MODEL` — default `gpt-4o-mini` (small, non-reasoning, in `AVAILABLE_MODELS`).
- `TEMPORAL_DECAY_HALF_LIFE_DAYS` — default `900` (≈2.5y; `weight = 0.5 ** (age_days / half_life)`).
- `QUERY_MAX_SUBQUERIES` / `ROUTER_MAX_TARGETS` — caps for expansion/decomposition (`4`) and routing targets (`3`).
- Reuses the S10 pre-work knobs: `RETRIEVAL_SEARCH_MODE`, `RERANKER_ENABLED`, `RERANKER_MODEL`, `RETRIEVAL_RECALL_TOP_K`, `RERANK_TOP_N`, `RRF_K`.

Session 12 vars (hand-written agent):
- `AGENT_MODEL` / `AGENT_REASONING_EFFORT` — default `gpt-5` / `medium` (both in `AVAILABLE_MODELS`; the demo script overrides them per run — `gpt-5-mini` for cheap loop debugging). Plain settings, NOT runtime-config: there is no live endpoint this session, only `scripts/run_agent_s12.py`.
- `AGENT_MAX_ITERATIONS` — default `10`. Loop safeguard (one iteration = one Responses API round-trip) on top of the natural stop.
- `AGENT_SEARCH_TOP_K` / `AGENT_SEARCH_DISTANCE_THRESHOLD` — default `5` / `0.6`. What the `search_budgets` tool passes to `retrieve()`.

Session 13 vars (LangGraph estimation graph):
- `GRAPH_EXTRACTION_MODEL` / `GRAPH_GENERATION_MODEL` — default `gpt-4o-mini` / `gpt-4o`. The extract/classify nodes are simple structured calls a small model handles; the consolidation node needs the stronger model to reliably populate the numeric `engineer_days` field (`gpt-4o-mini` tends to leave it null). Both in `AVAILABLE_MODELS`. Plain settings, not runtime-config. (Pre-exercise nodes.)
- `LOGFIRE_SERVICE_NAME` — default `estimator`. The Logfire service name. The token itself is read by Logfire from `LOGFIRE_TOKEN` in the environment; **no token ⇒ spans run locally but export nothing** (observability never breaks startup).
- The graph's Postgres checkpointer **reuses `DATABASE_URL`** (its tables coexist with pgvector) — no separate connection string.
- Live-session (multi-agent) knobs: `GRAPH_CLASSIFIER_MODEL` (default `gpt-4o-mini`, the classifier agent), `GRAPH_ANALYSIS_MODEL` / `GRAPH_PROPOSAL_MODEL` (default `gpt-4o`, the analysis + bonus proposal agents), `GRAPH_PROPOSAL_ENABLED` (default `true` — the conditional edge after gate 2 only drafts a proposal when this is on AND the human asked for one), and `GRAPH_STRUCTURE_EFFORT_BY_COMPLEXITY` (maps the classifier's `complexity` → the structure agent's reasoning effort). The structure + hours-recovery agents reuse `AGENT_MODEL` (gpt-5) and the `AGENT_*` / `TASK_HOURS_*` knobs. All plain settings, not runtime-config.

## Docker

Multi-stage Dockerfile: `builder` installs prod-only deps with `uv sync --no-install-project --no-dev`, `runtime` is a clean `python:3.11-slim` that only carries `/app/.venv` and `app/`, runs as non-root `appuser`. There is a Docker-native HEALTHCHECK against `/health`. `docker-compose.yml` bind-mounts `./app` and `./tests` for development; `--reload` is on. Redis service uses `redis/redis-stack:7.4.0-v0` for RediSearch.

For running tests inside the container the prod image lacks pytest. Two options:
```bash
# 1. Run on the host with uv
cd estimator && uv sync && uv run pytest

# 2. Install ad-hoc inside the container (lost on rebuild)
docker compose exec estimator bash -c '
  python -m ensurepip --upgrade && \
  python -m pip install --quiet pytest pytest-asyncio fakeredis httpx
'
docker compose exec estimator python -m pytest tests/ -v
```

## estimator-web (Rails)

Full guide in `estimator-web/README.md`. Consumes `POST /api/v1/estimate` and renders the structured `EstimationResponse`. Quick reference:

```bash
cd estimator-web
docker compose up --build               # http://localhost:3000

# Or with the FastAPI estimator (shared network):
cd /Users/antonioperez/projects/ia/ai-engineering
docker compose up --build
```

Common operations:

```bash
docker compose exec estimator-web bin/rails console
docker compose exec estimator-web bin/rails test
docker compose exec postgres psql -U postgres estimator_web_development
```

Design points to respect when editing (full layer map and rules in `estimator-web/ARCHITECTURE.md`):

- **The app is organized by contexts mirroring the Master's modules** — `estimation` (S04), `conversation` (S05), `rag` (S07) — over an `EstimatorAi` foundation (`app/services/estimator_ai/`: `BaseClient` + one client per context) that is the only layer talking HTTP to FastAPI. Contexts never import each other.
- **Contract POROs mirror the Pydantic schemas 1:1** (`from_hash` ↔ `model_validate`): `Estimation::Response.from_hash` builds nested `Estimation::Result` + `Estimation::Phase`; `Rag::ComparisonResponse.from_hash` builds the chunking-comparison tree. Views render the typed objects, not raw JSON; AR roots persist the full payload as JSONB.
- **The `Stimulus form_loading_controller`** is intentionally simple: it just disables the submit button and shows rolling phase messages while Rails waits for FastAPI. No SSE / no streaming — those were removed when the response became a single JSON object.
- **The cliente never talks to OpenAI / Anthropic directly.** It only POSTs to FastAPI, and the FastAPI handles guardrails, LLM calls and caches. That boundary is deliberate and documented in the session guide.
- **GuardrailViolation is a first-class error** in the cliente (`EstimatorAi::GuardrailViolation`, raised by `app/services/estimator_ai/base_client.rb`). The FastAPI returns 400 with `{detail: {reason, message}}` when input is rejected (moderation/prompt_injection/pii); the cliente surfaces this in `flash`.
- **The Chunking Lab** (`/rag/chunking_comparisons`, S07) compares chunking strategies via `POST /embeddings/compare` over the bundled corpus (`lib/estimator_ai/data/`); each run is persisted (`chunking_comparisons`) so paid strategies are never re-paid. Long calls pass a per-instance timeout (`EmbeddingsClient.new(timeout: 600)`) — the global 180s default stays.
- **`config/database.yml` reads `DATABASE_HOST` / `DATABASE_PORT` / `DATABASE_USER` / `DATABASE_PASSWORD` from ENV** with `nil` fallbacks (Unix socket when not in docker).
- **Kamal and Thruster** (`.kamal/`, `config/deploy.yml`, `bin/kamal`, `bin/thrust`, gems with `require: false`) are leftovers from `rails new`. Production is out of scope.
