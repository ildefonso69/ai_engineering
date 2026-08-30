# Mirror of the FastAPI ``GenerateResult``: the grounded estimate plus the
# grounding signals the wizard surfaces (fabricated citations / coherence).
module Rag
  class GenerationView
    attr_reader :estimate, :fabricated_source_ids, :coherent

    def self.from_hash(hash)
      hash = (hash || {}).transform_keys(&:to_s)
      new(
        estimate: Rag::EstimateView.from_hash(hash["estimate"]),
        fabricated_source_ids: Array(hash["fabricated_source_ids"]).map(&:to_i),
        coherent: hash.fetch("coherent", true)
      )
    end

    def initialize(estimate:, fabricated_source_ids:, coherent:)
      @estimate = estimate
      @fabricated_source_ids = fabricated_source_ids
      @coherent = coherent
    end

    def fabricated_citations? = fabricated_source_ids.any?

    def grounding_clean? = !fabricated_citations? && coherent
  end
end
