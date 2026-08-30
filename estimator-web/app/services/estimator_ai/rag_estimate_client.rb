# Context client for the Session 9 RAG estimation pipeline. Talks to the
# per-stage wizard endpoints (POST /v1/estimate/stages/*) and the full
# transcript → estimate endpoint, all guarded by the estimate API key
# (X-API-Key header, injected once via BaseClient#default_headers).
#
# Stateless contract: each stage takes the prior stage's artifacts and returns
# the next one, so the caller (the wizard controller) persists everything and
# can re-run a single stage in isolation.
module EstimatorAi
  class RagEstimateClient < BaseClient
    def initialize(timeout: Rails.application.config.estimator_ai.timeout)
      super(
        timeout: timeout,
        default_headers: { "X-API-Key" => Rails.application.config.estimator_ai.estimate_api_key }
      )
    end

    # Stage 1 — transcript → { "query" => {...}, "search_text" => "..." }.
    def reformulate(transcript:)
      handle_response(json_conn.post("/v1/estimate/stages/reformulate", { transcript: transcript }))
    end

    # Stage 2 — search text → RetrievalResult. The FastAPI schema field is
    # ``query_text``; the wizard feeds it the composed search text.
    def retrieve(search_text:, top_k: 10, distance_threshold: 0.6,
                 sectors: nil, project_year_min: nil, project_year_max: nil, chunk_types: nil)
      payload = {
        query_text: search_text,
        top_k: top_k,
        distance_threshold: distance_threshold,
        sectors: sectors.presence,
        project_year_min: project_year_min,
        project_year_max: project_year_max,
        chunk_types: chunk_types.presence
      }.compact
      handle_response(json_conn.post("/v1/estimate/stages/retrieve", payload))
    end

    # Stage 3 — chunks → assembled <source> context block (+ what fit the budget).
    def assemble(chunks:, max_context_tokens: nil)
      payload = { chunks: chunks, max_context_tokens: max_context_tokens }.compact
      handle_response(json_conn.post("/v1/estimate/stages/assemble", payload))
    end

    # Stage 4 — context block + query → grounded estimate + grounding signals.
    def generate(context_block:, query:, kept_chunks:)
      handle_response(json_conn.post("/v1/estimate/stages/generate", {
        context_block: context_block, query: query, kept_chunks: kept_chunks
      }))
    end

    # Full pipeline (single shot, idempotent). Kept for the "compare against the
    # one-shot path" demo; the wizard itself drives the stages above.
    def from_transcript(transcript:, idempotency_key: nil)
      payload = { transcript: transcript, idempotency_key: idempotency_key }.compact
      handle_response(json_conn.post("/v1/estimate/from-transcript", payload))
    end
  end
end
