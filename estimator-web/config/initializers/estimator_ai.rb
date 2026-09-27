Rails.application.config.estimator_ai = ActiveSupport::OrderedOptions.new.tap do |c|
  c.base_url = ENV.fetch("ESTIMATOR_API_BASE_URL", "http://localhost:8000")
  c.timeout  = ENV.fetch("ESTIMATOR_AI_TIMEOUT", "180").to_i

  # Session 9 RAG endpoints (X-API-Key header). Must match the FastAPI service's
  # RETRIEVAL_API_KEY / ESTIMATE_API_KEY. Blank disables the RAG wizard (401).
  c.retrieval_api_key = ENV.fetch("RETRIEVAL_API_KEY", nil)
  c.estimate_api_key  = ENV.fetch("ESTIMATE_API_KEY", nil)

  # Session 15: service-to-service token (X-Service-Token header) for
  # POST /api/v1/estimate. Must match the FastAPI service's AI_SERVICE_TOKEN.
  # Blank keeps that endpoint open (dev/test default — see security.py).
  # ENV can hand us an empty string (e.g. a root .env that declares the key
  # with no value) rather than a genuinely unset var — normalize "" to nil so
  # this side treats "blank" and "absent" identically, mirroring the
  # AI_SERVICE_TOKEN normalization in the FastAPI service's config.py.
  c.ai_service_token = ENV.fetch("AI_SERVICE_TOKEN", nil).presence
end
