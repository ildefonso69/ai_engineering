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

  GUARDRAIL_REASONS = %w[moderation prompt_injection pii].freeze

  class BaseClient
    def initialize(base_url: Rails.application.config.estimator_ai.base_url,
                   timeout:  Rails.application.config.estimator_ai.timeout,
                   default_headers: {})
      @base_url = base_url
      @timeout  = timeout
      # Headers applied to every request of this client (e.g. an X-API-Key for
      # the Session 9 endpoints). Empty by default → existing clients unchanged.
      @default_headers = default_headers
    end

    private

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
      when 404
        raise SessionNotFound, extract_detail(response.body).to_s
      when 415, 422
        raise InvalidRequest, extract_detail(response.body).to_s
      when 500
        # The embeddings endpoints return actionable 500 details (e.g. a
        # missing API key for a chunking strategy) — surface them.
        detail = extract_detail(response.body).to_s
        raise ServerError, detail.presence || "unexpected status 500"
      when 502
        raise ServerError, "Upstream LLM call failed"
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
