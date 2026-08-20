require "test_helper"
require "webmock/minitest"

module EstimatorAi
  # Session 15 — the status codes are part of the contract.
  #
  # The AI service distinguishes "I am broken" (500), "my upstream failed" (502)
  # and "a dependency I need is down" (503), plus 429 for the per-API-key rate
  # limit and 409 for "nothing pending to resume". Before this session all three
  # of 409/429/503 fell into the catch-all and surfaced as the useless
  # "unexpected status N", which erased the one piece of information the caller
  # needed: whether retrying was worth it.
  #
  # The pattern is independent of the stack: any HTTP client needs the same
  # branching to degrade gracefully.
  class BaseClientErrorsTest < ActiveSupport::TestCase
    setup do
      # The error taxonomy is defined inside base_client.rb, so Zeitwerk cannot
      # resolve EstimatorAi::ServiceUnavailable & co. from their own file names.
      # Touching the client class first loads them. (The controllers rely on the
      # same thing implicitly: a client always loads before an error is raised.)
      EstimatorAi::BaseClient

      WebMock.disable_net_connect!
      @request = Estimation::Request.new(
        description: "Mobile app with login, chat and push notifications",
        project_type: "mobile_app",
        detail_level: "medium",
        output_format: "phases_table"
      )
    end

    teardown do
      WebMock.reset!
      WebMock.allow_net_connect!
    end

    def estimate_returning(status:, body: {}, headers: {})
      stub_request(:post, "http://ai-test/api/v1/estimate").to_return(
        status: status,
        body: body.to_json,
        headers: { "Content-Type" => "application/json" }.merge(headers)
      )
      EstimatorAi::EstimationsClient.new(base_url: "http://ai-test").estimate(@request)
    end

    test "503 becomes ServiceUnavailable, not a generic ServerError" do
      error = assert_raises(EstimatorAi::ServiceUnavailable) do
        estimate_returning(status: 503, body: { detail: "Embedding service is not available." })
      end

      assert_match(/Embedding service is not available/, error.message)
      # It must NOT be swallowed by the ServerError branch: the two mean
      # different things to the caller (retry vs give up).
      assert_kind_of EstimatorAi::Error, error
      refute_kind_of EstimatorAi::ServerError, error
    end

    test "503 with an empty body still gets an actionable message" do
      error = assert_raises(EstimatorAi::ServiceUnavailable) do
        estimate_returning(status: 503, body: {})
      end

      assert_match(/dependency/i, error.message)
    end

    test "429 becomes RateLimited and carries Retry-After" do
      error = assert_raises(EstimatorAi::RateLimited) do
        estimate_returning(status: 429, body: { detail: "rate limit" },
                           headers: { "Retry-After" => "60" })
      end

      assert_equal 60, error.retry_after
      assert_match(/60s/, error.message)
    end

    test "429 without Retry-After still raises RateLimited" do
      error = assert_raises(EstimatorAi::RateLimited) do
        estimate_returning(status: 429, body: { detail: "rate limit" })
      end

      assert_nil error.retry_after
    end

    test "409 becomes Conflict instead of 'unexpected status 409'" do
      error = assert_raises(EstimatorAi::Conflict) do
        estimate_returning(status: 409, body: { detail: "No pending gate." })
      end

      assert_match(/No pending gate/, error.message)
      refute_match(/unexpected status/, error.message)
    end

    test "502 stays a ServerError so upstream LLM failures keep their meaning" do
      error = assert_raises(EstimatorAi::ServerError) do
        estimate_returning(status: 502, body: { detail: "whatever" })
      end

      assert_match(/Upstream LLM call failed/, error.message)
    end

    test "500 stays a ServerError and is not confused with 503" do
      error = assert_raises(EstimatorAi::ServerError) do
        estimate_returning(status: 500, body: { detail: "boom" })
      end

      refute_kind_of EstimatorAi::ServiceUnavailable, error
    end

    test "an unmapped status still falls back to the catch-all" do
      error = assert_raises(EstimatorAi::ServerError) do
        estimate_returning(status: 418, body: {})
      end

      assert_match(/unexpected status 418/, error.message)
    end
  end
end
