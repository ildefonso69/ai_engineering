require "test_helper"

# Session 16 — the guardrail verdict over the wizard's DERIVED hours.
#
# Twin of Rag::EstimateViewReviewTest, for the other shape the flag arrives in.
# Same field names on purpose: the platform routes on one predicate and should
# not have to know which endpoint produced the breakdown.
class Rag::TaskHoursReviewTest < ActiveSupport::TestCase
  def task(hours = 40)
    { "module" => "Auth", "task" => "OAuth", "estimated_hours" => hours, "has_match" => true }
  end

  test "carries the guardrail verdict and its reasons" do
    view = Rag::TaskHoursView.from_hash(
      "tasks" => [ task ],
      "requires_human_review" => true,
      "review_reasons" => [ "total of 300 engineer-days is 9.2x the 33 engineer-days of 2 distinct historical analogs" ]
    )

    assert view.needs_review?
    assert_equal 1, view.review_reasons.size
  end

  test "a breakdown with no tasks is not a review case" do
    # Nothing has been derived yet — the same reasoning that makes an abstention
    # on the RAG path not a review case.
    view = Rag::TaskHoursView.from_hash("tasks" => [], "requires_human_review" => true)

    assert_not view.needs_review?
  end

  test "hours from a service that predates the flag default to no review" do
    view = Rag::TaskHoursView.from_hash("tasks" => [ task ])

    assert_not view.needs_review?
    assert_equal [], view.review_reasons
  end
end
