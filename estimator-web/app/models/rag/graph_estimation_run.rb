# Session 13 — AR root of the GRAPH-driven estimation wizard.
#
# Where the Session 12 wizard (Rag::EstimationRun) choreographed the flow itself —
# calling one FastAPI stage endpoint per screen — this wizard delegates the whole
# orchestration to a LangGraph multi-agent graph inside the service IA. The graph
# PAUSES at two human gates; this row is the durable handle the business backend
# keeps so it can render each gate and RESUME the run when the person approves.
#
# The service IA owns the state (its Postgres checkpointer, keyed by ``estimation_id``
# == the graph thread_id). We mirror just enough here to render the current screen and
# to survive a pause of minutes or days: each ``GraphRunState`` the service returns is
# persisted into the JSONB columns below.
module Rag
  class GraphEstimationRun < ApplicationRecord
    self.table_name = "graph_estimation_runs"

    # The two human gates the graph pauses at, in order.
    GATE_STRUCTURE = "structure_review".freeze
    GATE_FINAL     = "final_review".freeze

    validates :transcript, presence: true
    validates :estimation_id, presence: true, uniqueness: true

    # The graph is executing a leg in the background (between two gates); the show
    # page renders the live per-agent panel and polls #progress until it pauses/ends.
    def running? = graph_state == "running"
    def paused? = graph_state == "paused"
    def completed? = graph_state == "completed"
    def at_structure_gate? = current_gate == GATE_STRUCTURE
    def at_final_gate? = current_gate == GATE_FINAL

    # The modules→tasks the graph proposed (reviewed at gate 1). Reuses the same
    # WorkModuleView the Session 12 wizard renders, so the editor partials are shared.
    def structure_modules
      Array(structure["modules"]).map { |raw| Rag::WorkModuleView.from_hash(raw) }
    end

    def structure? = structure.present? && structure["modules"].present?

    # The estimate the hours agent built (modules→tasks with engineer_hours), shown at
    # gate 2 and after completion.
    def estimate_modules
      Array(estimate["modules"]).map { |raw| Rag::WorkModuleView.from_hash(raw) }
    end

    def estimate? = estimate.present? && estimate["modules"].present?

    def total_engineer_days = estimate["total_engineer_days"].to_i

    def total_engineer_hours = estimate["total_engineer_hours"].to_f

    def analysis_report? = analysis_report.present? && analysis_report["summary"].present?

    def proposal? = proposal.present?

    # Merge a GraphRunState (from the service IA) into this row. One mapping used by
    # both start and resume, so the persisted shape never drifts from the contract.
    def apply_run_state!(run_state)
      run_state = run_state.to_h.stringify_keys
      gate = run_state["pending_gate"] || {}
      payload = (gate["payload"] || {})
      assign_attributes(
        graph_state: run_state["state"] || "paused",
        current_gate: gate["gate"],
        status: run_state["status"],
        pending_gate: gate,
        # At gate 1 the structure lives in the gate payload; afterwards it stays put.
        structure: payload["structure"] || structure,
        estimate: run_state["estimate"] || estimate,
        analysis_report: run_state["analysis_report"] || analysis_report,
        task_hours: { "tasks" => run_state["task_hours"] || task_hours["tasks"] || [] },
        proposal: run_state["proposal"] || proposal
      )
      save!
    end
  end
end
