require "test_helper"
require "webmock/minitest"

# Session 16 — the production-signals panel published through the business
# backend, because since Session 15 the AI service has no port of its own.
class RagDashboardControllerTest < ActionDispatch::IntegrationTest
  PANEL = "<!doctype html><html><body><h1>AI service — production signals</h1></body></html>".freeze

  setup do
    WebMock.disable_net_connect!
  end

  teardown do
    WebMock.reset!
    WebMock.allow_net_connect!
  end

  def stub_panel
    stub_request(:get, %r{/api/v1/eval/dashboard\z})
      .to_return(status: 200, body: PANEL, headers: { "Content-Type" => "text/html" })
  end

  def stub_data(generated_at: "2026-08-27T09:00:00+00:00")
    stub_request(:get, %r{/api/v1/eval/dashboard\.json\z})
      .to_return(status: 200,
                 body: { generated_at: generated_at, overall: {}, by_path: [] }.to_json,
                 headers: { "Content-Type" => "application/json" })
  end

  test "the framed page embeds the panel and says when it was generated" do
    stub_data

    get rag_dashboard_path

    assert_response :success
    assert_select "iframe[src=?]", rag_dashboard_raw_path
    assert_match(/2026-08-27T09:00:00/, response.body)
  end

  test "the raw action serves the panel untouched and without the layout" do
    # Untouched matters: the same file is what someone opens over SSH, and the
    # page carries its own styles and its own light/dark palette.
    stub_panel

    get rag_dashboard_raw_path

    assert_response :success
    assert_equal PANEL, response.body
    assert_no_match(/Estimator/, response.body) # no application chrome
  end

  test "a dead AI service degrades into a message, not a 500" do
    stub_request(:get, %r{/api/v1/eval/dashboard\.json\z}).to_timeout

    get rag_dashboard_path

    assert_response :success
    assert_match(/No se pudo hablar con el servicio IA/i, response.body)
  end

  test "the panel carries the service token like every other client" do
    previous = Rails.application.config.estimator_ai.service_token
    Rails.application.config.estimator_ai.service_token = "s16-token"
    stub_panel

    get rag_dashboard_raw_path

    assert_requested(:get, %r{/api/v1/eval/dashboard\z},
                     headers: { "X-Service-Token" => "s16-token" })
  ensure
    Rails.application.config.estimator_ai.service_token = previous
  end
end
