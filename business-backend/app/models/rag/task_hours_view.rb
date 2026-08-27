# Mirror of the FastAPI ``TaskHoursResult``: the per-task hours estimates for a
# whole run, plus convenience counts for the hours-review screen.
module Rag
  class TaskHoursView
    attr_reader :tasks, :review_reasons, :requires_human_review

    def self.from_hash(hash)
      new(hash || {})
    end

    def initialize(payload = {})
      stringified = (payload || {}).transform_keys(&:to_s)
      @tasks = Array(stringified["tasks"]).map { |raw| Rag::TaskHoursEstimateView.from_hash(raw) }
      # Session 16: the AI service's deterministic guardrail over these hours.
      # Absent on a payload produced before S16 — hence the explicit defaults
      # rather than a fetch: an old run must render, not raise.
      @requires_human_review = stringified["requires_human_review"] || false
      @review_reasons = Array(stringified["review_reasons"])
    end

    # A breakdown with no tasks has not been derived yet; that is not a review
    # case, the same way an abstention is not one on the RAG estimate path.
    def needs_review? = requires_human_review && tasks.any?

    def matched_count = tasks.count(&:has_match)

    def flagged_count = tasks.count(&:flagged?)

    def contradicted_count = tasks.count(&:contradicted?)

    def total_count = tasks.size
  end
end
