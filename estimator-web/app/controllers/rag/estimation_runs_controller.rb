# RAG estimation wizard (Session 9). Walks a transcript through the four
# pipeline stages — reformulation → retrieval → augmentation → generation —
# one screen at a time, then a human-verification step where the instructor
# edits the cost breakdown. Each stage is a member action that calls the
# matching FastAPI per-stage endpoint and persists its output on the run, so
# any stage can be re-run in isolation (e.g. retrieval with new filters).
#
# Synchronous, like the Chunking Lab: the heavy call is generation (gpt-5), so
# that one gets a longer timeout and the form_loading spinner.
module Rag
  class EstimationRunsController < ApplicationController
    GENERATE_TIMEOUT_SECONDS = 300
    ALLOWED_SECTORS = %w[finance ecommerce healthcare industrial].freeze

    def index
      @runs = Rag::EstimationRun.order(created_at: :desc).limit(20)
    end

    def new
      @run = Rag::EstimationRun.new
    end

    # Create the run and immediately run stage 1 (reformulation) so the wizard
    # opens on a populated screen.
    def create
      transcript = params.dig(:estimation_run, :transcript).to_s.strip
      @run = Rag::EstimationRun.new(transcript: transcript, idempotency_key: SecureRandom.uuid)
      unless @run.valid?
        flash.now[:alert] = "Pega una transcripción para empezar."
        return render :new, status: :unprocessable_entity
      end
      @run.save!
      guard_rag_errors do
        run_reformulation!
        redirect_to rag_estimation_run_path(@run, step: "reformulation")
      end
    end

    def show
      @run = Rag::EstimationRun.find(params[:id])
      @step = STEP_OR_CURRENT.call(@run, params[:step])
    end

    # --- stage member actions (each re-runnable) -----------------------------

    def reformulate
      @run = Rag::EstimationRun.find(params[:id])
      guard_rag_errors do
        run_reformulation!
        redirect_to rag_estimation_run_path(@run, step: "reformulation"),
                    notice: "Reformulación actualizada."
      end
    end

    def retrieve
      @run = Rag::EstimationRun.find(params[:id])
      guard_rag_errors do
        filters = retrieval_filters
        payload = rag_client.retrieve(
          search_text: @run.reformulation_view&.search_text.to_s,
          top_k: filters[:top_k],
          distance_threshold: filters[:distance_threshold],
          sectors: filters[:sectors],
          project_year_min: filters[:project_year_min],
          project_year_max: filters[:project_year_max],
          chunk_types: filters[:chunk_types]
        )
        @run.update!(
          retrieval: payload.merge("filters" => filters.transform_keys(&:to_s)),
          status: "retrieved",
          current_step: "retrieval"
        )
        @run.clear_downstream!("retrieval")
        redirect_to rag_estimation_run_path(@run, step: "retrieval")
      end
    end

    def assemble
      @run = Rag::EstimationRun.find(params[:id])
      guard_rag_errors do
        retrieval = @run.retrieval_view
        payload = rag_client.assemble(
          chunks: retrieval ? retrieval.chunk_payloads : [],
          max_context_tokens: params[:max_context_tokens].presence&.to_i
        )
        @run.update!(augmentation: payload, status: "assembled", current_step: "augmentation")
        @run.clear_downstream!("augmentation")
        redirect_to rag_estimation_run_path(@run, step: "augmentation")
      end
    end

    def generate
      @run = Rag::EstimationRun.find(params[:id])
      guard_rag_errors do
        augmentation = @run.augmentation_view
        query = @run.reformulation_view&.query
        payload = rag_client(timeout: GENERATE_TIMEOUT_SECONDS).generate(
          context_block: augmentation&.context_block.to_s,
          query: query ? query.to_payload : {},
          kept_chunks: augmentation ? augmentation.kept_chunk_payloads : []
        )
        @run.update!(
          generation: payload,
          adjusted_breakdown: seed_adjusted_breakdown(payload),
          status: "generated",
          current_step: "generation"
        )
        redirect_to rag_estimation_run_path(@run, step: "generation")
      end
    end

    # Human verification: persist the edited breakdown as a version distinct from
    # the immutable LLM original, recomputing the total authoritatively.
    def verify
      @run = Rag::EstimationRun.find(params[:id])
      modules = normalized_modules
      total = modules.sum { |m| m["tasks"].sum { |t| t["engineer_days"] } }
      @run.update!(
        adjusted_breakdown: {
          "modules" => modules,
          "total_engineer_days" => total,
          "adjusted_at" => Time.current.iso8601
        },
        status: "verified",
        current_step: "verification"
      )
      redirect_to rag_estimation_run_path(@run, step: "verification"),
                  notice: "Estimación verificada y guardada."
    end

    private

    STEP_OR_CURRENT = lambda do |run, requested|
      step = requested.presence || run.current_step
      Rag::EstimationRun::STEPS.include?(step) ? step : "transcript"
    end

    def rag_client(timeout: Rails.application.config.estimator_ai.timeout)
      EstimatorAi::RagEstimateClient.new(timeout: timeout)
    end

    def run_reformulation!
      payload = rag_client.reformulate(transcript: @run.transcript)
      @run.update!(reformulation: payload, status: "reformulated", current_step: "reformulation")
      @run.clear_downstream!("reformulation")
    end

    def retrieval_filters
      {
        top_k: params[:top_k].presence&.to_i&.clamp(1, 30) || 10,
        distance_threshold: params[:distance_threshold].presence&.to_f&.clamp(0.0, 2.0) || 0.6,
        sectors: Array(params[:sectors]).select { |s| ALLOWED_SECTORS.include?(s) }.presence,
        project_year_min: params[:project_year_min].presence&.to_i,
        project_year_max: params[:project_year_max].presence&.to_i,
        chunk_types: Array(params[:chunk_types]).map(&:to_s).compact_blank.presence
      }
    end

    # Start the editable table as a copy of the LLM modular breakdown (empty when
    # the estimate is insufficient — the user can add modules/tasks manually).
    def seed_adjusted_breakdown(generation_payload)
      estimate = generation_payload.fetch("estimate", {})
      modules = Array(estimate["modules"]).map do |m|
        m = m.transform_keys(&:to_s)
        {
          "name" => m["name"].to_s,
          "description" => m["description"].to_s,
          "tasks" => Array(m["tasks"]).map { |t| seed_task(t) }
        }
      end
      {
        "modules" => modules,
        "total_engineer_days" => total_of(modules),
        "adjusted_at" => nil # nil = not yet human-verified, just the seeded copy
      }
    end

    def seed_task(raw)
      raw = raw.transform_keys(&:to_s)
      { "name" => raw["name"].to_s, "description" => raw["description"].to_s,
        "engineer_days" => raw["engineer_days"].to_i,
        "sources" => Array(raw["sources"]).map(&:to_i) }
    end

    # Parse the nested modules→tasks params (integer-indexed hashes from the
    # Stimulus editor). Drops modules/tasks with a blank name.
    def normalized_modules
      raw_modules = params[:modules]
      return [] if raw_modules.blank?

      values_of(raw_modules).filter_map do |raw_module|
        attrs = to_h(raw_module)
        name = attrs["name"].to_s.strip
        next if name.blank?

        tasks = values_of(attrs["tasks"]).filter_map { |t| normalized_task(t) }
        { "name" => name, "description" => attrs["description"].to_s, "tasks" => tasks }
      end
    end

    def normalized_task(raw_task)
      attrs = to_h(raw_task)
      name = attrs["name"].to_s.strip
      return nil if name.blank?

      { "name" => name, "description" => attrs["description"].to_s,
        "engineer_days" => attrs["engineer_days"].to_i,
        "sources" => parse_sources(attrs["sources"]) }
    end

    def total_of(modules)
      modules.sum { |m| Array(m["tasks"]).sum { |t| t["engineer_days"].to_i } }
    end

    # Integer-indexed params arrive as a hash {"0" => {...}, "1" => {...}};
    # take the values in index order. Tolerates a plain array too.
    def values_of(collection)
      h = to_h(collection)
      h.is_a?(Hash) ? h.sort_by { |k, _| k.to_i }.map(&:last) : Array(collection)
    end

    def to_h(obj)
      obj.respond_to?(:to_unsafe_h) ? obj.to_unsafe_h : (obj || {})
    end

    def parse_sources(value)
      value.to_s.split(/[,\s]+/).map(&:to_i).select(&:positive?)
    end

    # --- error handling (mirrors the Chunking Lab posture) -------------------
    # Inline rescue (not rescue_from): the EstimatorAi error taxonomy lives in
    # base_client.rb, so the constants only resolve once a client has loaded —
    # which always happens inside the yielded block before any error is raised.
    def guard_rag_errors
      yield
    rescue EstimatorAi::InvalidRequest => e
      redirect_back_to_run("Petición inválida: #{e.message}")
    rescue EstimatorAi::ServerError => e
      message = e.message.to_s
      hint =
        if message.include?("API key") || message.include?("401")
          "¿Coinciden RETRIEVAL_API_KEY / ESTIMATE_API_KEY con las del servicio IA?"
        elsif message.include?("not available")
          "El servicio de embeddings/LLM no está disponible en el estimator (¿falta la API key?)."
        else
          "Error del servicio IA: #{message}"
        end
      redirect_back_to_run(hint)
    rescue Faraday::TimeoutError, Faraday::ConnectionFailed => e
      redirect_back_to_run("El servicio IA no respondió a tiempo. La generación con gpt-5 puede " \
                           "tardar; reintenta. (#{e.class})")
    end

    def redirect_back_to_run(message)
      flash[:alert] = message
      if @run&.persisted?
        redirect_to rag_estimation_run_path(@run, step: @run.current_step)
      else
        redirect_to new_rag_estimation_run_path
      end
    end
  end
end
