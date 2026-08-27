# Session 16 — the human-review flag, promoted from JSONB to a column.
#
# The flag itself is produced by the AI service's deterministic guardrail and
# already travels inside the per-stage JSONB. It gets a column because the point
# of the flag is to LOCATE the runs that carry it, and a listing cannot filter on
# something buried in a document without an index that hard-codes where in the
# document it lives. Same shape as supervisor_estimation_runs (Session 14), which
# has real columns for exactly the same reason.
#
# Derived data, so it has exactly one writer per table (``sync_review_flag!`` /
# ``apply_run_state!``); anything else and the badge and the banner start
# disagreeing.
class AddRequiresHumanReviewToEstimationRuns < ActiveRecord::Migration[8.0]
  def change
    add_column :estimation_runs, :requires_human_review, :boolean,
               default: false, null: false
    add_column :graph_estimation_runs, :requires_human_review, :boolean,
               default: false, null: false

    # Partial indexes: the review queue is a minority by design, so indexing only
    # the true rows keeps both indexes tiny.
    add_index :estimation_runs, :requires_human_review,
              where: "requires_human_review",
              name: "index_estimation_runs_on_requires_human_review"
    add_index :graph_estimation_runs, :requires_human_review,
              where: "requires_human_review",
              name: "index_graph_estimation_runs_on_requires_human_review"
  end
end
