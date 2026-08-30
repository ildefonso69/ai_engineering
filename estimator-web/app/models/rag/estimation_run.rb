# ActiveRecord root of the Session 9 RAG wizard. Persists one transcript →
# grounded-estimate run as it advances stage by stage: each pipeline stage
# writes its FastAPI response into a JSONB column, and ``current_step`` tracks
# which screen the wizard host renders. Mirrors the Rag::ChunkingComparison
# pattern (AR root + JSONB + PORO views).
module Rag
  class EstimationRun < ApplicationRecord
    self.table_name = "estimation_runs"

    # Ordered wizard steps. ``transcript`` is the entry form; the rest map 1:1
    # to a pipeline stage and to a JSONB column of the same family.
    STEPS = %w[transcript reformulation retrieval augmentation generation verification].freeze

    # Stage → the JSONB column that must be populated for that step to render.
    STEP_COLUMNS = {
      "reformulation" => :reformulation,
      "retrieval"     => :retrieval,
      "augmentation"  => :augmentation,
      "generation"    => :generation,
      "verification"  => :generation # verification renders on top of generation
    }.freeze

    validates :transcript, presence: true

    # --- typed views over the per-stage JSONB --------------------------------

    def reformulation_view
      Rag::ReformulationView.from_hash(reformulation) if reformulation.present?
    end

    def retrieval_view
      Rag::RetrievalView.from_hash(retrieval) if retrieval.present?
    end

    def augmentation_view
      Rag::AugmentationView.from_hash(augmentation) if augmentation.present?
    end

    def generation_view
      Rag::GenerationView.from_hash(generation) if generation.present?
    end

    # The human-verified breakdown ({ "modules" => [...],
    # "total_engineer_days" => n, "adjusted_at" => iso }). Falsy until saved.
    def adjusted_modules
      Array(adjusted_breakdown["modules"]).map { |raw| Rag::WorkModuleView.from_hash(raw) }
    end

    def adjusted_total = adjusted_breakdown["total_engineer_days"].to_i

    def adjusted? = adjusted_breakdown.present? && adjusted_breakdown["modules"].present?

    # --- wizard state --------------------------------------------------------

    def step_complete?(step)
      column = STEP_COLUMNS[step.to_s]
      column ? self[column].present? : true
    end

    # When an earlier stage is re-run, downstream artifacts no longer match the
    # new input — null them so the wizard never shows a stale context block or
    # estimate built from superseded chunks.
    def clear_downstream!(from_step)
      idx = STEPS.index(from_step.to_s)
      return if idx.nil?

      cleared = {}
      STEPS[(idx + 1)..].each do |step|
        column = STEP_COLUMNS[step]
        cleared[column] = {} if column && self.class.column_names.include?(column.to_s)
      end
      # verification shares the generation column; clearing generation is enough.
      cleared.delete(:generation) if from_step.to_s == "generation"
      update!(cleared) if cleared.any?
    end
  end
end
