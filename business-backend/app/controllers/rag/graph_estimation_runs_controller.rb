# Session 13 — the GRAPH-driven estimation wizard.
#
# The whole orchestration lives in the service IA as a LangGraph multi-agent graph;
# this controller only does what a business backend must: START the run, render each
# human gate the graph pauses at, and RESUME the run with the person's decision. Three
# HTTP verbs against the service, one per human touch-point:
#
#   create        → POST /v1/estimate/graph                      (start → gate 1)
#   resume_structure → POST /v1/estimate/graph/:id/resume        (gate 1 → gate 2)
#   resume_final     → POST /v1/estimate/graph/:id/resume        (gate 2 → done)
#
# The graph may sit paused for minutes or days between these calls — its state is held
# by the service's Postgres checkpointer, mirrored here into the run row. The pattern
# is stack-agnostic: any HTTP client could drive the same resumes.
module Rag
  class GraphEstimationRunsController < ApplicationController
    def index
      @runs = Rag::GraphEstimationRun.order(created_at: :desc).limit(20)
    end

    def new
      @run = Rag::GraphEstimationRun.new
    end

    # START the graph: runs the classifier + structure agents and pauses at gate 1.
    def create
      transcript = params.dig(:graph_estimation_run, :transcript).to_s.strip
      estimation_id = SecureRandom.uuid
      @run = Rag::GraphEstimationRun.new(transcript: transcript, estimation_id: estimation_id)
      unless @run.valid?
        flash.now[:alert] = "Pega una transcripción para empezar."
        return render :new, status: :unprocessable_entity
      end
      # Persist the run BEFORE calling the service, so a guardrail rejection (or a
      # timeout) leaves a row the person can reopen and retry — same posture as S12.
      @run.save!

      guard_graph_errors do
        # Kick the graph off in the BACKGROUND (202) and go straight to the live panel;
        # the classifier + structure agents report their progress there as they run.
        graph_client.graph_start_stream(transcript: transcript, estimation_id: estimation_id)
        @run.update!(graph_state: "running")
        redirect_to rag_graph_estimation_run_path(@run),
                    notice: "Grafo iniciado. Sigue en vivo lo que hace cada agente."
      end
    end

    def show
      @run = Rag::GraphEstimationRun.find(params[:id])
    end

    # HUMAN GATE 1 → resume with the reviewed module→task breakdown (in background).
    def resume_structure
      @run = Rag::GraphEstimationRun.find(params[:id])
      decision = { "approved" => true, "modules" => reviewed_modules }
      guard_graph_errors do
        graph_client.graph_resume_stream(estimation_id: @run.estimation_id, decision: decision)
        @run.update!(graph_state: "running")
        redirect_to rag_graph_estimation_run_path(@run),
                    notice: "Estructura aprobada. Sigue en vivo las horas y el análisis."
      end
    end

    # HUMAN GATE 2 → resume with the final validation (+ optional proposal, in background).
    # The human may have completed/adjusted per-task hours; we patch the stored estimate
    # by index (the structure is fixed at gate 2) and send it as estimate_overrides. The
    # service recomputes totals/confidence from the edited hours.
    def resume_final
      @run = Rag::GraphEstimationRun.find(params[:id])
      decision = {
        "validated" => true,
        "want_proposal" => ActiveModel::Type::Boolean.new.cast(params[:want_proposal]) || false,
        "estimate_overrides" => { "modules" => estimate_modules_with_edited_hours }
      }
      guard_graph_errors do
        graph_client.graph_resume_stream(estimation_id: @run.estimation_id, decision: decision)
        @run.update!(graph_state: "running")
        redirect_to rag_graph_estimation_run_path(@run),
                    notice: "Estimación validada. Redactando el cierre en vivo…"
      end
    end

    # Draft (or re-draft) the commercial proposal after completion — over the run's
    # validated estimate, no graph re-run. Available even if it was not asked for at gate 2.
    def generate_proposal
      @run = Rag::GraphEstimationRun.find(params[:id])
      guard_graph_errors do
        proposal = graph_client.graph_proposal(estimation_id: @run.estimation_id)
        @run.update!(proposal: proposal["body_markdown"], proposal_title: proposal["title"])
        redirect_to rag_graph_estimation_run_path(@run),
                    notice: "Propuesta redactada por el Arquitecto."
      end
    end

    # Download the proposal as a basic PDF (Prawn). 302 back if there is no proposal yet.
    def proposal_pdf
      @run = Rag::GraphEstimationRun.find(params[:id])
      unless @run.proposal?
        return redirect_to rag_graph_estimation_run_path(@run),
                           alert: "Aún no hay propuesta. Genérala primero."
      end
      send_data Rag::ProposalPdf.new(@run).render,
                type: "application/pdf",
                filename: "propuesta-#{@run.id}.pdf",
                disposition: "inline"
    end

    # LIVE POLL (JSON) — the graph-progress Stimulus controller hits this every ~1.5s
    # while a leg runs. Returns the per-agent activity feed; on a terminal state it
    # persists the artifacts so the reload renders the gate / completed screen.
    def progress
      @run = Rag::GraphEstimationRun.find(params[:id])
      data = graph_client.graph_progress(estimation_id: @run.estimation_id)
      state = data["state"]
      finished = state != "running"
      @run.apply_run_state!(data) if finished
      render json: { finished: finished, state: state, activity: data["activity"] || [] }
    rescue EstimatorAi::Error, Faraday::Error
      # Transient error mid-run — keep the poller alive (mirrors index_runs#status).
      render json: { finished: false, state: "running", activity: [] }
    end

    private

    def graph_client(timeout: Rails.application.config.estimator_ai.timeout)
      EstimatorAi::RagEstimateClient.new(timeout: timeout)
    end

    # Patch the stored estimate's module→task tree with the hours the human edited at
    # gate 2 (matched BY INDEX — the structure is read-only there, so indices align 1:1).
    # Only ``estimated_hours`` changes; name/description/has_match/reliability are kept
    # from the estimate, and the service recomputes the totals from the new hours.
    def estimate_modules_with_edited_hours
      Array((@run.estimate || {})["modules"]).each_with_index.map do |mod, m|
        tasks = Array(mod["tasks"]).each_with_index.map do |task, t|
          raw = params.dig(:modules, m.to_s, :tasks, t.to_s, :estimated_hours).to_s.strip
          task.merge("estimated_hours" => (raw.blank? ? nil : raw.to_f))
        end
        mod.merge("tasks" => tasks)
      end
    end

    # Parse the integer-indexed module→task params from the shared editor into the
    # ``[{ name:, tasks: [{ name:, description: }] }]`` shape the resume decision wants.
    def reviewed_modules
      values_of(params[:modules]).filter_map do |raw_module|
        attrs = to_h(raw_module)
        name = attrs["name"].to_s.strip
        next if name.blank?

        tasks = values_of(attrs["tasks"]).filter_map do |t|
          ta = to_h(t)
          tname = ta["name"].to_s.strip
          next if tname.blank?

          { "name" => tname, "description" => ta["description"].to_s }
        end
        { "name" => name, "tasks" => tasks }
      end
    end

    def values_of(collection)
      h = to_h(collection)
      h.is_a?(Hash) ? h.sort_by { |k, _| k.to_i }.map(&:last) : Array(collection)
    end

    def to_h(obj)
      obj.respond_to?(:to_unsafe_h) ? obj.to_unsafe_h : (obj || {})
    end

    # Mirrors the Session 9/12 wizard's error posture (GuardrailViolation, timeouts).
    def guard_graph_errors
      yield
    rescue EstimatorAi::GuardrailViolation => e
      redirect_back_to_run("Entrada rechazada por guardarraíles: #{e.message}")
    rescue EstimatorAi::InvalidRequest => e
      redirect_back_to_run("Petición inválida: #{e.message}")
    rescue EstimatorAi::Unauthorized => e
      # Session 15: this used to escape the wrapper entirely and render a Rails
      # 500 page, hiding an actionable message. A 401 here is nearly always a
      # configuration mismatch between the two services, not a runtime failure.
      redirect_back_to_run("El servicio IA rechazó nuestras credenciales. " \
                           "Revisa AI_SERVICE_TOKEN en ambos servicios. (#{e.message})")
    rescue EstimatorAi::ServiceUnavailable => e
      # Session 15: transient by definition (vector DB / Redis / embedder down),
      # so the copy invites a retry instead of reporting a broken system.
      redirect_back_to_run("Una dependencia del servicio IA no está disponible " \
                           "ahora mismo; reintenta en unos segundos. (#{e.message})")
    rescue EstimatorAi::RateLimited => e
      redirect_back_to_run("Has alcanzado el límite de peticiones del servicio IA. " \
                           "#{e.retry_after ? "Reintenta en #{e.retry_after}s." : 'Reintenta en un momento.'}")
    rescue EstimatorAi::Conflict => e
      redirect_back_to_run("La ejecución no está en un punto que admita esa acción: #{e.message}")
    rescue EstimatorAi::ServerError => e
      redirect_back_to_run("Error del servicio IA: #{e.message}")
    rescue Faraday::TimeoutError, Faraday::ConnectionFailed => e
      redirect_back_to_run("El servicio IA no respondió a tiempo. Los agentes gpt-5 pueden " \
                           "tardar; reintenta. (#{e.class})")
    end

    def redirect_back_to_run(message)
      flash[:alert] = message
      if @run&.persisted?
        redirect_to rag_graph_estimation_run_path(@run)
      else
        redirect_to new_rag_graph_estimation_run_path
      end
    end
  end
end
