Rails.application.config.estimator_ai = ActiveSupport::OrderedOptions.new.tap do |c|
  c.base_url = ENV.fetch("ESTIMATOR_API_BASE_URL", "http://localhost:8000")
  c.timeout  = ENV.fetch("ESTIMATOR_AI_TIMEOUT", "180").to_i

  # Session 9 RAG endpoints (X-API-Key header). Must match the FastAPI service's
  # RETRIEVAL_API_KEY / ESTIMATE_API_KEY. Blank disables the RAG wizard (401).
  c.retrieval_api_key = ENV.fetch("RETRIEVAL_API_KEY", nil)
  c.estimate_api_key  = ENV.fetch("ESTIMATE_API_KEY", nil)
end
