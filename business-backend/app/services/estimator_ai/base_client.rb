require "faraday"
require "faraday/multipart"

# Foundation of the EstimatorAi namespace: the ONLY layer that talks HTTP to
# the FastAPI service. Every context client (EstimationsClient, SessionsClient,
# EmbeddingsClient, IngestionClient) inherits the Faraday connections, the
# response → typed-error mapping and the shared error taxonomy from here.
#
# Mirrors the estimator's ``foundation/`` layer: no business logic, no opinion
# about what the payloads mean — that belongs to the context POROs.
module EstimatorAi
  # Shared error taxonomy. Contexts rescue these (never Faraday directly,
  # except the transport-level ConnectionFailed/TimeoutError).
  Error              = Class.new(StandardError)
  InvalidRequest     = Class.new(Error)
  GuardrailViolation = Class.new(Error)
  SessionNotFound    = Class.new(Error)
  ServerError        = Class.new(Error)
  # Session 15: the AI service rejected our credentials — either the shared
  # X-Service-Token or a Session 9 X-API-Key. Almost always a configuration
  # mismatch between the two services rather than a runtime failure, so it gets
  # its own class instead of being lumped into ServerError.
  Unauthorized       = Class.new(Error)
  # Session 15: the AI service is up but a dependency it needs is not (vector
  # database, Redis, embedder). Distinct from ServerError because it is
  # TRANSIENT — the right reaction is to retry or degrade, not to give up.
  ServiceUnavailable = Class.new(Error)
  # Session 15: per-API-key rate limit (Session 9 routers). Carries the
  # Retry-After the service sent, so a caller can honour it.
  RateLimited        = Class.new(Error) do
    attr_reader :retry_after

    def initialize(message = nil, retry_after: nil)
      super(message)
      @retry_after = retry_after
    end
  end
  # Session 15: resume/proposal called when there is nothing pending, or a
  # duplicate ingest. The request was understood and refused — retrying it
  # unchanged will fail again.
  Conflict           = Class.new(Error)

  GUARDRAIL_REASONS = %w[moderation prompt_injection pii].freeze

  # Header carrying the shared service token (Session 15). Sent on every
  # request; the AI service enforces it as middleware for the whole app.
  SERVICE_TOKEN_HEADER = "X-Service-Token".freeze

  class BaseClient
    def initialize(base_url: Rails.application.config.estimator_ai.base_url,
                   timeout:  Rails.application.config.estimator_ai.timeout,
                   default_headers: {})
      @base_url = base_url
      @timeout  = timeout
      # Headers applied to every request of this client. The service token is
      # merged in for ALL clients (the AI service checks it app-wide), while a
      # subclass may add its own — e.g. RagEstimateClient's X-API-Key for the
      # Session 9 routers. Subclass headers win on conflict.
      @default_headers = service_token_header.merge(default_headers)
    end

    private

    # Blank token → no header. That is the normal state when the AI service runs
    # locally with the check disabled, and sending an empty header would be
    # indistinguishable from sending a wrong one.
    def service_token_header
      token = Rails.application.config.estimator_ai.service_token
      return {} if token.blank?

      { SERVICE_TOKEN_HEADER => token }
    end

    def json_conn
      @json_conn ||= Faraday.new(url: @base_url) do |f|
        f.request  :json
        f.response :json
        f.headers.update(@default_headers)
        f.options.timeout = @timeout
        f.adapter Faraday.default_adapter
      end
    end

    def multipart_conn
      @multipart_conn ||= Faraday.new(url: @base_url) do |f|
        f.request  :multipart
        f.request  :url_encoded
        f.response :json
        # Session 15: this line was missing. Multipart requests went out with no
        # default headers at all, so they carried neither the service token nor
        # any X-API-Key. It only ever hit unauthenticated endpoints, so it stayed
        # invisible until the token became mandatory app-wide.
        f.headers.update(@default_headers)
        f.options.timeout = @timeout
        f.adapter Faraday.default_adapter
      end
    end

    def handle_response(response)
      case response.status
      when 200, 202
        # 202 Accepted: the async graph *stream verbs return the initial "running"
        # progress; the body is still the parsed JSON we want.
        response.body
      when 400
        detail = extract_detail(response.body)
        reason = detail.is_a?(Hash) ? detail["reason"] : nil
        if GUARDRAIL_REASONS.include?(reason)
          message = detail.is_a?(Hash) ? detail["message"] || reason : reason
          raise GuardrailViolation, "Input rejected (#{reason}): #{message}"
        else
          raise InvalidRequest, detail.to_s
        end
      when 401
        # Session 15. Before this branch existed a 401 fell through to the
        # catch-all and surfaced as "unexpected status 401", which said nothing
        # about the real cause. The message names both possibilities because the
        # two auth layers are independent and either can be the culprit.
        detail = extract_detail(response.body)
        reason = detail.is_a?(Hash) ? detail["reason"] : nil
        raise Unauthorized,
              "AI service rejected our credentials (#{reason || 'unauthorized'}). " \
              "Check that AI_SERVICE_TOKEN matches on both services, and that " \
              "ESTIMATE_API_KEY / RETRIEVAL_API_KEY are set for the RAG endpoints."
      when 404
        raise SessionNotFound, extract_detail(response.body).to_s
      when 409
        # Session 15. Before this branch a 409 surfaced as "unexpected status
        # 409", which hid a perfectly meaningful answer: the graph/supervisor
        # resume endpoints return it when no gate is pending.
        raise Conflict, extract_detail(response.body).to_s
      when 415, 422
        raise InvalidRequest, extract_detail(response.body).to_s
      when 429
        # Session 15. The Session 9 routers are rate limited per API key and
        # send Retry-After; surfacing it lets the caller wait the right amount
        # instead of hammering.
        retry_after = response.headers["Retry-After"].presence&.to_i
        raise RateLimited.new(
          "AI service rate limit reached#{" (retry in #{retry_after}s)" if retry_after}.",
          retry_after: retry_after
        )
      when 500
        # The embeddings endpoints return actionable 500 details (e.g. a
        # missing API key for a chunking strategy) — surface them.
        detail = extract_detail(response.body).to_s
        raise ServerError, detail.presence || "unexpected status 500"
      when 502
        raise ServerError, "Upstream LLM call failed"
      when 503
        # Session 15. The AI service distinguishes "I am broken" (500) from "a
        # dependency I need is down" (503) — the vector database, Redis or the
        # embedder. Only the second one is worth retrying, so it gets its own
        # class instead of collapsing into ServerError.
        detail = extract_detail(response.body)
        # An empty JSON body parses to {}, whose to_s is the non-blank "{}" —
        # so check the parsed value, not its string form, before falling back.
        message = detail.is_a?(Hash) && detail.empty? ? nil : detail.to_s.presence
        raise ServiceUnavailable,
              message || "A dependency of the AI service is unavailable."
      else
        raise ServerError, "unexpected status #{response.status}"
      end
    end

    def extract_detail(body)
      return body unless body.is_a?(Hash)
      body["detail"] || body
    end
  end
end
