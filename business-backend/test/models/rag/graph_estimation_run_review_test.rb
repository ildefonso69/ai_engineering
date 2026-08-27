require "test_helper"

# Session 16 — the flag is mirrored out of the estimate JSONB into a column so the
# listing can find it, and ``apply_run_state!`` is its only writer.
class Rag::GraphEstimationRunReviewTest < ActiveSupport::TestCase
  def run_state(estimate)
    {
      "estimation_id" => SecureRandom.uuid,
      "state" => "completed",
      "status" => "needs_review",
      "estimate" => estimate
    }
  end

  def new_run
    Rag::GraphEstimationRun.create!(transcript: "…", estimation_id: SecureRandom.uuid)
  end

  test "a flagged estimate lights up the column and the scope" do
    run = new_run
    run.apply_run_state!(run_state(
      "modules" => [], "total_engineer_days" => 300,
      "requires_human_review" => true,
      "review_reasons" => [ "9.2x the 2 distinct historical analogs" ]
    ))

    assert run.reload.requires_human_review
    assert run.needs_review?
    assert_equal [ "9.2x the 2 distinct historical analogs" ], run.review_reasons
    assert_includes Rag::GraphEstimationRun.needs_review, run
  end

  test "a clean estimate leaves the column false" do
    run = new_run
    run.apply_run_state!(run_state("modules" => [], "requires_human_review" => false))

    assert_not run.reload.requires_human_review
    assert_not_includes Rag::GraphEstimationRun.needs_review, run
  end

  test "a human clearing the condition at gate 2 clears the badge" do
    # The reason the verdict is re-derived after the gate rather than carried: a
    # stale "needs review" on an estimate the reviewer already fixed is how a team
    # learns to click past the banner.
    run = new_run
    run.apply_run_state!(run_state("modules" => [], "requires_human_review" => true,
                                   "review_reasons" => [ "3 of 4 tasks have no hours behind them" ]))
    assert run.reload.requires_human_review

    run.apply_run_state!(run_state("modules" => [], "requires_human_review" => false,
                                   "review_reasons" => []))

    assert_not run.reload.requires_human_review
  end

  test "a run from a service that predates the flag is not flagged" do
    run = new_run
    run.apply_run_state!(run_state("modules" => [], "total_engineer_days" => 82))

    assert_not run.reload.requires_human_review
    assert_equal [], run.review_reasons
  end
end
