require "test_helper"

# Session 16 — the platform routes on the AI service's guardrail verdict; it does
# not re-derive it.
class Rag::EstimateViewReviewTest < ActiveSupport::TestCase
  test "carries the guardrail verdict and its reasons" do
    view = Rag::EstimateView.from_hash(
      "total_engineer_days" => 566,
      "confidence" => "low",
      "reasoning" => "…",
      "requires_human_review" => true,
      "review_reasons" => [ "total of 566 engineer-days is 7.4x the retrieved evidence" ]
    )

    assert view.needs_review?
    assert_equal 1, view.review_reasons.size
  end

  test "an abstention is never a review case" do
    # The system declining to answer is the system working. Routing every
    # abstention to a person trains reviewers to rubber-stamp the whole queue.
    view = Rag::EstimateView.from_hash(
      "confidence" => "insufficient",
      "reasoning" => "…",
      "requires_human_review" => true
    )

    assert view.insufficient?
    assert_not view.needs_review?
  end

  test "an estimate from a service that predates the flag defaults to no review" do
    # Backwards compatibility with a deployed image built before Session 16: the
    # keys are simply absent, and absent must mean "nothing to escalate" rather
    # than a nil that blows up in the view.
    view = Rag::EstimateView.from_hash("total_engineer_days" => 82, "confidence" => "high")

    assert_not view.needs_review?
    assert_equal [], view.review_reasons
  end
end
