require "test_helper"
require "webmock/minitest"

# Session 13 (live) — the graph wizard drives the service IA's *background* streaming
# contract: create/resume kick the graph off with the *stream verbs (202 "running")
# and the show page polls #progress until a leg pauses at a gate or completes. We stub
# the FastAPI endpoints with WebMock and assert the controller: starts in the
# background, feeds the poll, persists each terminal GraphRunState, and sends the key.
class RagGraphEstimationRunsControllerTest < ActionDispatch::IntegrationTest
  GRAPH = "/v1/estimate/graph".freeze

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

  # --- fixtures: the /progress payloads (GraphProgress = GraphRunState + state + activity) ---
  def running(activity = [])
    { estimation_id: "irrelevant", state: "running", activity: activity }
  end

  def paused_at_structure
    {
      estimation_id: "irrelevant", state: "paused",
      activity: [ { seq: 0, node: "classifier", label: "Classifier", message: "Complejidad: high" },
                  { seq: 1, node: "structure", label: "Structure", message: "1 módulos · 1 tareas" } ],
      pending_gate: {
        gate: "structure_review", estimation_id: "irrelevant",
        payload: { structure: { modules: [
          { name: "Backend", tasks: [ { name: "API", description: "REST API" } ] }
        ] } }
      },
      structure: nil, task_hours: [], estimate: nil, analysis_report: nil, proposal: nil, errors: []
    }
  end

  def paused_at_final
    {
      estimation_id: "irrelevant", state: "paused", activity: [],
      pending_gate: { gate: "final_review", estimation_id: "irrelevant", payload: {} },
      estimate: { modules: [ { name: "Backend", tasks: [ { name: "API", estimated_hours: 40, has_match: true } ] } ],
                  total_engineer_days: 5, total_engineer_hours: 40.0, confidence: "high" },
      task_hours: [ { module: "Backend", task: "API", estimated_hours: 40, has_match: true } ],
      analysis_report: { overall_confidence: "high", grounded_task_ratio: 1.0, weak_points: [], summary: "solid" },
      proposal: nil, errors: []
    }
  end

  def completed
    {
      estimation_id: "irrelevant", state: "completed", pending_gate: nil, status: "validated", activity: [],
      estimate: { modules: [ { name: "Backend", tasks: [ { name: "API", estimated_hours: 40, has_match: true } ] } ],
                  total_engineer_days: 5, total_engineer_hours: 40.0, confidence: "high" },
      analysis_report: { overall_confidence: "high", grounded_task_ratio: 1.0, weak_points: [], summary: "solid" },
      task_hours: [ { module: "Backend", task: "API", estimated_hours: 40, has_match: true } ],
      proposal: "# Proposal\nA solid backend.", errors: []
    }
  end

  def json(body, status: 200)
    { status: status, body: body.to_json, headers: { "Content-Type" => "application/json" } }
  end

  # A run already sitting in a background leg, ready to be polled.
  def running_run
    Rag::GraphEstimationRun.create!(transcript: "x" * 150, estimation_id: SecureRandom.uuid,
                                    graph_state: "running")
  end

  test "new renders the transcript form" do
    get new_rag_graph_estimation_run_path
    assert_response :success
    assert_select "textarea[name='graph_estimation_run[transcript]']"
  end

  test "create starts the graph in the background and shows the live panel" do
    stub = stub_request(:post, %r{#{GRAPH}/stream\z}).to_return(json(running, status: 202))
    assert_difference -> { Rag::GraphEstimationRun.count }, 1 do
      post rag_graph_estimation_runs_path, params: { graph_estimation_run: { transcript: "x" * 150 } }
    end
    run = Rag::GraphEstimationRun.order(:id).last
    assert_redirected_to rag_graph_estimation_run_path(run)
    assert run.running?, "run should be running while the leg executes in the background"
    assert_requested(:post, %r{#{GRAPH}/stream\z}, headers: { "X-API-Key" => "test-estimate-key" })
    assert_requested stub

    get rag_graph_estimation_run_path(run)
    assert_response :success
    assert_match "Flujo en vivo", response.body
  end

  test "progress passes through the live activity while a leg runs" do
    run = running_run
    stub_request(:get, %r{#{GRAPH}/#{run.estimation_id}/progress\z})
      .to_return(json(running([ { seq: 0, node: "classifier", label: "Classifier", message: "Complejidad: high" } ])))

    get progress_rag_graph_estimation_run_path(run)
    assert_response :success
    body = JSON.parse(response.body)
    assert_equal false, body["finished"]
    assert_equal "running", body["state"]
    assert_equal "Complejidad: high", body.dig("activity", 0, "message")
    assert run.reload.running?, "an unfinished leg must not mutate the run state"
  end

  test "progress persists the terminal paused-at-gate-1 state and reports finished" do
    run = running_run
    stub_request(:get, %r{#{GRAPH}/#{run.estimation_id}/progress\z}).to_return(json(paused_at_structure))

    get progress_rag_graph_estimation_run_path(run)
    assert_response :success
    body = JSON.parse(response.body)
    assert_equal true, body["finished"]
    assert_equal "paused", body["state"]

    run.reload
    assert run.paused?
    assert run.at_structure_gate?
    assert_equal 1, run.structure_modules.size
  end

  test "resume_structure resumes the leg in the background" do
    run = Rag::GraphEstimationRun.create!(transcript: "x" * 150, estimation_id: SecureRandom.uuid,
                                          graph_state: "paused", current_gate: "structure_review")
    resume = stub_request(:post, %r{#{GRAPH}/#{run.estimation_id}/resume-stream\z})
             .to_return(json(running, status: 202))

    post resume_structure_rag_graph_estimation_run_path(run),
         params: { modules: { "0" => { name: "Backend", tasks: { "0" => { name: "API", description: "REST" } } } } }

    assert_redirected_to rag_graph_estimation_run_path(run)
    assert run.reload.running?
    assert_requested(:post, %r{#{GRAPH}/#{run.estimation_id}/resume-stream\z}) do |req|
      body = JSON.parse(req.body)
      body.dig("decision", "approved") == true &&
        body.dig("decision", "modules", 0, "name") == "Backend"
    end
    assert_requested resume
  end

  test "resume_final resumes in the background carrying the proposal choice" do
    run = Rag::GraphEstimationRun.create!(transcript: "x" * 150, estimation_id: SecureRandom.uuid,
                                          graph_state: "paused", current_gate: "final_review")
    stub_request(:post, %r{#{GRAPH}/#{run.estimation_id}/resume-stream\z}).to_return(json(running, status: 202))

    post resume_final_rag_graph_estimation_run_path(run), params: { want_proposal: "1" }

    assert run.reload.running?
    assert_requested(:post, %r{#{GRAPH}/#{run.estimation_id}/resume-stream\z}) do |req|
      body = JSON.parse(req.body)
      body.dig("decision", "validated") == true && body.dig("decision", "want_proposal") == true
    end
  end

  test "resume_final sends the human-edited per-task hours as estimate_overrides" do
    run = Rag::GraphEstimationRun.create!(
      transcript: "x" * 150, estimation_id: SecureRandom.uuid, graph_state: "paused", current_gate: "final_review",
      estimate: { "modules" => [ { "name" => "Backend", "tasks" => [
        { "name" => "API", "estimated_hours" => 40, "has_match" => true, "reliability" => 0.9 },
        { "name" => "Facturación", "estimated_hours" => nil, "has_match" => false } ] } ],
        "total_engineer_days" => 5, "total_engineer_hours" => 40.0, "confidence" => "medium" }
    )
    captured = nil
    stub_request(:post, %r{#{GRAPH}/#{run.estimation_id}/resume-stream\z})
      .with { |req| captured = JSON.parse(req.body); true }
      .to_return(json(running, status: 202))

    post resume_final_rag_graph_estimation_run_path(run), params: {
      want_proposal: "1",
      modules: { "0" => { name: "Backend", tasks: {
        "0" => { estimated_hours: "48" },   # adjusted
        "1" => { estimated_hours: "24" } } } } # filled the previously-missing task
    }

    assert run.reload.running?
    dec = captured["decision"]
    assert_equal true, dec["validated"]
    mods = dec.dig("estimate_overrides", "modules")
    assert_equal 48.0, mods[0]["tasks"][0]["estimated_hours"]
    assert_equal 24.0, mods[0]["tasks"][1]["estimated_hours"]
    # Non-hours fields are preserved from the stored estimate (patch by index).
    assert_equal "API", mods[0]["tasks"][0]["name"]
    assert_equal false, mods[0]["tasks"][1]["has_match"]
  end

  test "resume_final leaves an unfilled task's hours nil" do
    run = Rag::GraphEstimationRun.create!(
      transcript: "x" * 150, estimation_id: SecureRandom.uuid, graph_state: "paused", current_gate: "final_review",
      estimate: { "modules" => [ { "name" => "M", "tasks" => [ { "name" => "T", "estimated_hours" => nil, "has_match" => false } ] } ] }
    )
    captured = nil
    stub_request(:post, %r{#{GRAPH}/#{run.estimation_id}/resume-stream\z})
      .with { |req| captured = JSON.parse(req.body); true }.to_return(json(running, status: 202))

    post resume_final_rag_graph_estimation_run_path(run),
         params: { modules: { "0" => { name: "M", tasks: { "0" => { estimated_hours: "" } } } } }

    assert_nil captured.dig("decision", "estimate_overrides", "modules", 0, "tasks", 0, "estimated_hours")
  end

  test "progress completes the run with a proposal" do
    run = running_run
    stub_request(:get, %r{#{GRAPH}/#{run.estimation_id}/progress\z}).to_return(json(completed))

    get progress_rag_graph_estimation_run_path(run)
    body = JSON.parse(response.body)
    assert_equal true, body["finished"]
    assert_equal "completed", body["state"]

    run.reload
    assert run.completed?
    assert_equal "validated", run.status
    assert run.proposal?
  end

  test "progress keeps the poller alive on a transient service error" do
    run = running_run
    stub_request(:get, %r{#{GRAPH}/#{run.estimation_id}/progress\z}).to_return(status: 502, body: "boom")

    get progress_rag_graph_estimation_run_path(run)
    assert_response :success
    assert_equal false, JSON.parse(response.body)["finished"]
    assert run.reload.running?
  end

  # A completed run with an estimate + reliability report (and optionally a proposal).
  def completed_run(proposal: nil)
    Rag::GraphEstimationRun.create!(
      transcript: "x" * 150, estimation_id: SecureRandom.uuid, graph_state: "completed", status: "validated",
      estimate: { "modules" => [ { "name" => "Backend", "tasks" => [ { "name" => "API", "estimated_hours" => 40, "has_match" => true } ] } ],
                  "total_engineer_days" => 5, "total_engineer_hours" => 40.0, "confidence" => "high" },
      analysis_report: { "overall_confidence" => "high", "grounded_task_ratio" => 1.0,
                         "weak_points" => [ { "severity" => "low", "area" => "Backend", "issue" => "minor gap" } ],
                         "summary" => "El Oráculo dice: estimación sólida en conjunto." },
      proposal: proposal, proposal_title: (proposal ? "Propuesta NÓVA" : nil)
    )
  end

  test "the completed screen shows the reliability report" do
    run = completed_run
    get rag_graph_estimation_run_path(run)
    assert_response :success
    assert_match "Informe de fiabilidad", response.body
    assert_match "El Oráculo dice: estimación sólida", response.body
    assert_match "minor gap", response.body
  end

  test "generate_proposal drafts and persists the proposal" do
    run = completed_run
    stub_request(:post, %r{#{GRAPH}/#{run.estimation_id}/proposal\z}).to_return(json(
      { estimation_id: run.estimation_id, title: "Propuesta NÓVA", executive_summary: "Resumen.",
        scope: [ "Backend" ], total_engineer_days: 5, body_markdown: "# Propuesta\nCuerpo." }
    ))
    post generate_proposal_rag_graph_estimation_run_path(run)
    assert_redirected_to rag_graph_estimation_run_path(run)
    run.reload
    assert run.proposal?
    assert_equal "Propuesta NÓVA", run.proposal_title
    assert_match "Cuerpo.", run.proposal
    assert_requested(:post, %r{#{GRAPH}/#{run.estimation_id}/proposal\z},
                     headers: { "X-API-Key" => "test-estimate-key" })
  end

  test "proposal_pdf downloads a PDF" do
    run = completed_run(proposal: "## Resumen\nCuerpo de la propuesta con acento é.")
    get proposal_pdf_rag_graph_estimation_run_path(run)
    assert_response :success
    assert_equal "application/pdf", response.media_type
    assert response.body.start_with?("%PDF"), "expected a PDF payload"
  end

  test "proposal_pdf redirects when there is no proposal yet" do
    run = completed_run
    get proposal_pdf_rag_graph_estimation_run_path(run)
    assert_redirected_to rag_graph_estimation_run_path(run)
    assert_match(/genérala/i, flash[:alert])
  end

  test "a guardrail violation on start is surfaced as a flash alert" do
    stub_request(:post, %r{#{GRAPH}/stream\z}).to_return(
      status: 400,
      body: { detail: { reason: "prompt_injection", message: "nope" } }.to_json,
      headers: { "Content-Type" => "application/json" }
    )
    post rag_graph_estimation_runs_path, params: { graph_estimation_run: { transcript: "x" * 150 } }
    run = Rag::GraphEstimationRun.order(:id).last
    assert_redirected_to rag_graph_estimation_run_path(run)
    assert_match(/guardarra/i, flash[:alert])
  end
end
