require "test_helper"

class Rag::EstimationRunTest < ActiveSupport::TestCase
  def chunk(id)
    { "id" => id, "content" => "c#{id}", "sector" => "finance",
      "project_year" => 2024, "chunk_type" => "line_item", "distance" => 0.4 }
  end

  test "requires a transcript" do
    assert_not Rag::EstimationRun.new(transcript: "").valid?
    assert Rag::EstimationRun.new(transcript: "hello").valid?
  end

  test "per-stage views round-trip from JSONB" do
    run = Rag::EstimationRun.create!(
      transcript: "t",
      reformulation: { "query" => { "function" => "store", "sector" => "ecommerce",
                                    "technologies" => [ "Stripe" ] }, "search_text" => "store" },
      retrieval: { "chunks" => [ chunk(1) ], "low_confidence" => false,
                   "candidates_evaluated" => 5, "filters" => { "top_k" => 10 } },
      generation: { "estimate" => { "confidence" => "high", "reasoning" => "r",
                    "modules" => [ { "name" => "Auth", "tasks" => [
                      { "name" => "OAuth", "engineer_days" => 8, "sources" => [ 1 ] },
                      { "name" => "RBAC", "engineer_days" => 4, "sources" => [ 1 ] } ] } ] },
                    "fabricated_source_ids" => [ 99 ], "coherent" => true }
    )

    assert_equal "ecommerce", run.reformulation_view.query.sector
    assert_equal [ "Stripe" ], run.reformulation_view.query.technologies
    assert_equal 1, run.retrieval_view.chunks.first.id
    assert_not run.retrieval_view.soft_fail?
    assert_equal 12, run.generation_view.estimate.recompute_total
    assert_equal 12, run.generation_view.estimate.modules.first.subtotal
    assert run.generation_view.fabricated_citations?
    assert_not run.generation_view.grounding_clean?
  end

  test "blank stage columns return nil views" do
    run = Rag::EstimationRun.create!(transcript: "t")
    assert_nil run.reformulation_view
    assert_nil run.retrieval_view
    assert_nil run.generation_view
  end

  test "clear_downstream! nulls later stage columns" do
    run = Rag::EstimationRun.create!(
      transcript: "t",
      reformulation: { "query" => {}, "search_text" => "s" },
      retrieval: { "chunks" => [ chunk(1) ], "low_confidence" => false, "candidates_evaluated" => 1 },
      augmentation: { "context_block" => "x", "kept_chunks" => [], "dropped_count" => 0, "token_count" => 1 },
      generation: { "estimate" => { "confidence" => "low", "reasoning" => "r" } }
    )

    run.clear_downstream!("retrieval")
    run.reload
    assert run.retrieval.present?, "retrieval itself is kept"
    assert run.augmentation.blank?, "augmentation cleared"
    assert run.generation.blank?, "generation cleared"
  end

  test "insufficient estimate exposes no numbers" do
    run = Rag::EstimationRun.create!(
      transcript: "t",
      generation: { "estimate" => { "confidence" => "insufficient", "reasoning" => "r",
                    "insufficient_context_explanation" => "nothing relevant" },
                    "fabricated_source_ids" => [], "coherent" => true }
    )
    estimate = run.generation_view.estimate
    assert estimate.insufficient?
    assert_nil estimate.total_engineer_days
    assert estimate.modules.empty?
  end
end
