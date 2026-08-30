require "test_helper"
require "webmock/minitest"

class RagEstimationRunsControllerTest < ActionDispatch::IntegrationTest
  STAGES = "/v1/estimate/stages".freeze

  setup do
    WebMock.disable_net_connect!
    @previous_key = Rails.application.config.estimator_ai.estimate_api_key
    Rails.application.config.estimator_ai.estimate_api_key = "test-estimate-key"
  end

  teardown do
    Rails.application.config.estimator_ai.estimate_api_key = @previous_key
    WebMock.reset!
    WebMock.allow_net_connect!
  end

  def stub_reformulate(query: { "function" => "online store", "sector" => "ecommerce", "scale" => "medium" })
    stub_request(:post, %r{#{STAGES}/reformulate})
      .to_return(status: 200,
                 body: { query: query, search_text: "online store card checkout ecommerce" }.to_json,
                 headers: { "Content-Type" => "application/json" })
  end

  def chunk(id)
    { "id" => id, "content" => "Auth component ~#{id} eng-days", "sector" => "ecommerce",
      "project_year" => 2024, "chunk_type" => "budget_component", "distance" => 0.3 + id / 100.0 }
  end

  def stub_retrieve(chunks: [ chunk(1), chunk(2) ], low_confidence: false, candidates: 12)
    stub_request(:post, %r{#{STAGES}/retrieve})
      .to_return(status: 200,
                 body: { chunks: chunks, low_confidence: low_confidence, candidates_evaluated: candidates }.to_json,
                 headers: { "Content-Type" => "application/json" })
  end

  def run_with_reformulation
    stub_reformulate
    transcript = "x" * 150
    post rag_estimation_runs_path, params: { estimation_run: { transcript: transcript } }
    Rag::EstimationRun.order(:id).last
  end

  # --- new / create ----------------------------------------------------------

  test "new renders the transcript form" do
    get new_rag_estimation_run_path
    assert_response :success
    assert_select "textarea[name='estimation_run[transcript]']"
  end

  test "create persists the run, runs reformulation and sends the API key" do
    stub = stub_reformulate
    assert_difference -> { Rag::EstimationRun.count }, 1 do
      post rag_estimation_runs_path, params: { estimation_run: { transcript: "x" * 150 } }
    end
    run = Rag::EstimationRun.order(:id).last
    assert_redirected_to rag_estimation_run_path(run, step: "reformulation")
    assert_equal "ecommerce", run.reformulation_view.query.sector
    assert_equal "reformulated", run.status
    assert_requested(:post, %r{#{STAGES}/reformulate}, headers: { "X-API-Key" => "test-estimate-key" })
    assert_requested stub
  end

  test "create with a blank transcript re-renders" do
    post rag_estimation_runs_path, params: { estimation_run: { transcript: "" } }
    assert_response :unprocessable_entity
    assert_no_difference -> { Rag::EstimationRun.count } do
      post rag_estimation_runs_path, params: { estimation_run: { transcript: "  " } }
    end
  end

  # --- retrieve --------------------------------------------------------------

  test "retrieve persists chunks and the filters used" do
    run = run_with_reformulation
    stub_retrieve
    post retrieve_rag_estimation_run_path(run),
         params: { top_k: 5, distance_threshold: 0.5, sectors: [ "ecommerce" ] }
    run.reload
    assert_equal 2, run.retrieval_view.chunks.size
    assert_equal 5, run.retrieval["filters"]["top_k"]
    assert_equal [ "ecommerce" ], run.retrieval["filters"]["sectors"]
    assert_redirected_to rag_estimation_run_path(run, step: "retrieval")
  end

  test "soft-fail retrieval renders the alert and blocks continue" do
    run = run_with_reformulation
    stub_retrieve(chunks: [], low_confidence: true, candidates: 9)
    post retrieve_rag_estimation_run_path(run), params: { distance_threshold: 0.2 }
    follow_redirect!
    assert_response :success
    assert_match "Soft-fail", response.body
    assert_no_match "Continuar → Augmentation", response.body
  end

  # --- assemble --------------------------------------------------------------

  test "assemble persists the context block" do
    run = run_with_reformulation
    stub_retrieve
    post retrieve_rag_estimation_run_path(run), params: {}
    run.reload

    stub_request(:post, %r{#{STAGES}/assemble})
      .to_return(status: 200,
                 body: { context_block: "<source id=\"1\">x</source>", kept_chunks: [ chunk(1) ],
                         dropped_count: 1, token_count: 42 }.to_json,
                 headers: { "Content-Type" => "application/json" })

    post assemble_rag_estimation_run_path(run), params: {}
    run.reload
    assert_equal 42, run.augmentation_view.token_count
    assert_equal 1, run.augmentation_view.dropped_count
    assert_redirected_to rag_estimation_run_path(run, step: "augmentation")
  end

  # --- generate --------------------------------------------------------------

  def stub_generate(estimate:, fabricated: [], coherent: true)
    stub_request(:post, %r{#{STAGES}/generate})
      .to_return(status: 200,
                 body: { estimate: estimate, fabricated_source_ids: fabricated, coherent: coherent }.to_json,
                 headers: { "Content-Type" => "application/json" })
  end

  test "generate persists the estimate and seeds the editable breakdown" do
    run = run_with_reformulation
    run.update!(augmentation: { "context_block" => "<source/>", "kept_chunks" => [ chunk(1) ],
                                "dropped_count" => 0, "token_count" => 10 })
    estimate = {
      "total_engineer_days" => 18, "confidence" => "high", "reasoning" => "from sources",
      "modules" => [
        { "name" => "Auth", "tasks" => [ { "name" => "OAuth", "engineer_days" => 12, "sources" => [ 1 ] } ] },
        { "name" => "Checkout", "tasks" => [ { "name" => "Cart", "engineer_days" => 6, "sources" => [ 2 ] } ] }
      ],
      "sources" => [], "assumptions" => []
    }
    stub_generate(estimate: estimate)

    post generate_rag_estimation_run_path(run), params: {}
    run.reload
    assert_equal 18, run.generation_view.estimate.total_engineer_days
    # Editable table seeded as a copy of the LLM modular breakdown.
    assert_equal 2, run.adjusted_modules.size
    assert_equal 18, run.adjusted_total
    assert_redirected_to rag_estimation_run_path(run, step: "generation")
  end

  # --- verify ----------------------------------------------------------------

  test "verify recomputes the total server-side and persists the nested adjusted version" do
    run = Rag::EstimationRun.create!(transcript: "x" * 150,
      generation: { "estimate" => { "confidence" => "high", "reasoning" => "r",
        "modules" => [ { "name" => "Auth", "tasks" => [
          { "name" => "OAuth", "engineer_days" => 12, "sources" => [ 1 ] } ] } ] },
        "fabricated_source_ids" => [], "coherent" => true })

    # Nested, integer-indexed params (as the Stimulus editor serialises them).
    patch verify_rag_estimation_run_path(run), params: {
      modules: {
        "0" => { name: "Auth", description: "Access", tasks: {
          "0" => { name: "OAuth", engineer_days: "20", sources: "1, 2" },
          "1" => { name: "RBAC", engineer_days: "8", sources: "" },
          "2" => { name: "", engineer_days: "99", sources: "" } # blank task dropped
        } },
        "1" => { name: "", tasks: {} } # blank module dropped
      }
    }
    run.reload
    assert_equal "verified", run.status
    assert_equal 1, run.adjusted_modules.size
    assert_equal 2, run.adjusted_modules.first.tasks.size
    assert_equal 28, run.adjusted_total # 20 + 8, blanks dropped, recomputed server-side
    assert_equal [ 1, 2 ], run.adjusted_modules.first.tasks.first.sources
    assert run.adjusted_breakdown["adjusted_at"].present?
  end

  # --- error mapping ---------------------------------------------------------

  test "a 502 from a stage surfaces as a flash on redirect" do
    run = run_with_reformulation
    stub_request(:post, %r{#{STAGES}/retrieve})
      .to_return(status: 502, body: { detail: "Retrieval failed." }.to_json,
                 headers: { "Content-Type" => "application/json" })
    post retrieve_rag_estimation_run_path(run), params: {}
    assert_response :redirect
    follow_redirect!
    assert_match(/servicio IA/i, response.body)
  end

  # --- show renders every wizard step ----------------------------------------

  test "show renders augmentation, generation and verification screens" do
    run = Rag::EstimationRun.create!(
      transcript: "x" * 200,
      reformulation: { "query" => { "function" => "online store", "sector" => "ecommerce",
                                    "scale" => "medium", "technologies" => [ "Stripe" ] },
                       "search_text" => "online store stripe" },
      retrieval: { "chunks" => [ chunk(1) ], "low_confidence" => false, "candidates_evaluated" => 12,
                   "filters" => { "top_k" => 10, "distance_threshold" => 0.6 } },
      augmentation: { "context_block" => "<source id=\"1\">Auth</source>", "kept_chunks" => [ chunk(1) ],
                      "dropped_count" => 2, "token_count" => 120 },
      generation: { "estimate" => { "total_engineer_days" => 18, "duration_weeks" => 6,
                    "confidence" => "high", "reasoning" => "Derived from sources",
                    "modules" => [ { "name" => "Auth", "description" => "Access", "tasks" => [
                      { "name" => "OAuth", "engineer_days" => 12, "sources" => [ 1 ] } ] } ],
                    "sources" => [ { "source_id" => 1, "relevance" => "primary", "used_for" => "auth" } ],
                    "assumptions" => [ { "description" => "No SSO", "impact" => "low", "rationale" => "n/a" } ] },
                    "fabricated_source_ids" => [], "coherent" => true },
      adjusted_breakdown: { "modules" => [ { "name" => "Auth", "tasks" => [
                              { "name" => "OAuth", "engineer_days" => 12, "sources" => [ 1 ] } ] } ],
                            "total_engineer_days" => 12, "adjusted_at" => nil }
    )

    get rag_estimation_run_path(run, step: "augmentation")
    assert_response :success
    assert_match "&lt;source id=", response.body # XML block, html-escaped
    assert_match "Tokens del contexto", response.body

    get rag_estimation_run_path(run, step: "generation")
    assert_response :success
    assert_match "eng-días", response.body
    assert_match "Citaciones", response.body

    get rag_estimation_run_path(run, step: "verification")
    assert_response :success
    assert_select "input[name='modules[0][name]']"
    assert_select "input[name='modules[0][tasks][0][name]']"
    assert_select "template[data-estimate-modules-editor-target='moduleTemplate']"
    assert_select "template[data-estimate-modules-editor-target='taskTemplate']"
    assert_match "Original del LLM", response.body
  end

  test "show renders the grounding warning when citations are fabricated" do
    run = Rag::EstimationRun.create!(
      transcript: "x" * 200,
      augmentation: { "context_block" => "<source id=\"1\">x</source>", "kept_chunks" => [ chunk(1) ],
                      "dropped_count" => 0, "token_count" => 10 },
      generation: { "estimate" => { "confidence" => "high", "reasoning" => "r",
                    "modules" => [], "sources" => [], "assumptions" => [] },
                    "fabricated_source_ids" => [ 999 ], "coherent" => true }
    )
    get rag_estimation_run_path(run, step: "generation")
    assert_response :success
    assert_match "Citaciones inventadas", response.body
    assert_match "999", response.body
  end

  # --- index -----------------------------------------------------------------

  test "index lists persisted runs" do
    run_with_reformulation
    get rag_estimation_runs_path
    assert_response :success
    assert_match "Estimaciones guiadas", response.body
  end
end
