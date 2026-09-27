# Context client for the transactional estimation flow (Session 4).
# One endpoint: free-text in, validated structured estimation out.
#
# Session 15: sends the service-to-service token (X-Service-Token header) so
# the call is authenticated when running under the root docker-compose, where
# estimator is not reachable from the host at all.
module EstimatorAi
  class EstimationsClient < BaseClient
    def initialize(base_url: Rails.application.config.estimator_ai.base_url,
                    timeout:  Rails.application.config.estimator_ai.timeout)
      token = Rails.application.config.estimator_ai.ai_service_token
      # Omit the header entirely when there's no token, rather than sending
      # it with a nil value (unverified behaviour through Faraday/Net::HTTP).
      super(
        base_url: base_url,
        timeout: timeout,
        default_headers: token ? { "X-Service-Token" => token } : {}
      )
    end

    # ``request`` is an Estimation::Request (the Pydantic EstimationRequest mirror).
    def estimate(request)
      raise ArgumentError, "request must be valid" unless request.valid?

      response = json_conn.post("/api/v1/estimate", request.to_payload)
      handle_response(response)
    end
  end
end
