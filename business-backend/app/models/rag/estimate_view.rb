# Mirror of the FastAPI ``Estimate`` schema (S09, engineer-days + citations).
# Distinct from Estimation::Result (the S04 euros/weeks/phases contract).
module Rag
  class EstimateView
    include ActiveModel::Model
    include ActiveModel::Attributes

    attribute :total_engineer_days, :integer
    attribute :duration_weeks, :integer
    attribute :confidence, :string   # high | medium | low | insufficient
    attribute :reasoning, :string
    attribute :insufficient_context_explanation, :string
    # Session 16: written by the AI service's deterministic output guardrail,
    # never by the model. The platform's job is to ROUTE on it, not to re-derive
    # it — re-deciding here would give two answers to one question, and the one
    # in the audit log would be the other one.
    attribute :requires_human_review, :boolean, default: false

    attr_reader :modules, :sources, :assumptions, :review_reasons

    def self.from_hash(hash)
      new(hash || {})
    end

    def initialize(attributes = {})
      stringified = (attributes || {}).transform_keys(&:to_s)
      @modules = Array(stringified.delete("modules"))
        .map { |raw| Rag::WorkModuleView.from_hash(raw) }
      @sources = Array(stringified.delete("sources"))
        .map { |raw| Rag::SourceCitationView.from_hash(raw) }
      @assumptions = Array(stringified.delete("assumptions"))
        .map { |raw| Rag::AssumptionView.from_hash(raw) }
      @review_reasons = Array(stringified.delete("review_reasons"))
      super(stringified.slice(
        "total_engineer_days", "duration_weeks", "confidence",
        "reasoning", "insufficient_context_explanation", "requires_human_review"
      ))
    end

    def insufficient? = confidence == "insufficient"

    # An abstention is NOT a review case: the system declining to answer is the
    # system working, and routing every abstention to a person trains reviewers
    # to rubber-stamp. Only a delivered number the guardrail doubts gets a human.
    # ``requires_human_review`` (no ``?``): ActiveModel::Attributes types a
    # boolean but does not generate a predicate for it.
    def needs_review? = requires_human_review && !insufficient?

    # Authoritative sum of all tasks across all modules (used to sanity-check the
    # LLM total and as the basis for the human-verified total).
    def recompute_total = modules.sum(&:subtotal)
  end
end
