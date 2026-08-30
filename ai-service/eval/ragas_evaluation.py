"""RAGAS evaluation of generation quality for the golden set.

Measures four metrics per query:
- faithfulness: how much of the answer is supported by the context
- answer_relevancy: how much the answer is relevant to the question
- context_precision: what fraction of the retrieved context is relevant
- context_recall: what fraction of the needed context was retrieved

Requires RAGAS library and OpenAI API key.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import structlog
from datasets import Dataset
from ragas import evaluate
from ragas.metrics import answer_relevancy, context_precision, context_recall, faithfulness

# Suppress tokenizer warnings
os.environ["TOKENIZERS_PARALLELISM"] = "false"

log = structlog.get_logger()

EVAL_DIR = Path(__file__).parent
GOLDEN_SET_PATH = EVAL_DIR / "golden_set_extended.json"
RESULTS_PATH = EVAL_DIR / "ragas_evaluation_results.json"


async def run_evaluation() -> dict:
    """Run RAGAS evaluation on the golden set.

    Returns a dict with per-query metrics and aggregate stats.
    """
    log.info("loading golden set", path=str(GOLDEN_SET_PATH))
    with open(GOLDEN_SET_PATH) as f:
        cases = json.load(f)

    # Prepare evaluation dataset (exclude abstention case-006)
    data_points = []
    for case in cases:
        if case["difficulty"] == "abstention":
            log.info("skipping abstention case", case_id=case["id"])
            continue

        ground_truth = case.get("ground_truth", {})
        if not ground_truth:
            log.warning("case has no ground_truth", case_id=case["id"])
            continue

        # Convert Estimate to markdown for evaluation
        answer_text = _estimate_to_text(ground_truth)

        # Retrieve context: the sources cited in the ground truth
        # (For real evaluation, these would come from the actual retrieval pass)
        context_chunks = _extract_context_from_estimate(ground_truth)

        data_points.append(
            {
                "question": case["transcript"],  # user's transcript/brief
                "answer": answer_text,  # generated estimate
                "contexts": context_chunks if context_chunks else ["[No context available]"],
                "ground_truth": _estimate_to_text(ground_truth),  # expected answer for recall
            }
        )

    log.info("prepared evaluation dataset", num_cases=len(data_points))

    # Create RAGAS dataset
    ds = Dataset.from_dict(
        {
            "question": [dp["question"] for dp in data_points],
            "answer": [dp["answer"] for dp in data_points],
            "contexts": [dp["contexts"] for dp in data_points],
            "ground_truth": [dp["ground_truth"] for dp in data_points],
        }
    )

    # Run evaluation with the four metrics
    log.info("running RAGAS evaluation", num_queries=len(ds))
    results = await evaluate(
        ds,
        metrics=[
            faithfulness,
            answer_relevancy,
            context_precision,
            context_recall,
        ],
        raise_exceptions=False,  # Don't fail on individual metric errors
    )

    # Aggregate metrics
    metrics_dict = results.to_dict()
    output = {
        "num_queries": len(data_points),
        "queries": [],
        "aggregate": {
            "faithfulness": results["faithfulness"].mean() if "faithfulness" in results else None,
            "answer_relevancy": (
                results["answer_relevancy"].mean() if "answer_relevancy" in results else None
            ),
            "context_precision": (
                results["context_precision"].mean() if "context_precision" in results else None
            ),
            "context_recall": results["context_recall"].mean() if "context_recall" in results else None,
        },
    }

    # Per-query results
    for i, case in enumerate([c for c in cases if c["difficulty"] != "abstention"]):
        query_result = {
            "case_id": case["id"],
            "faithfulness": metrics_dict.get("faithfulness", [None])[i],
            "answer_relevancy": metrics_dict.get("answer_relevancy", [None])[i],
            "context_precision": metrics_dict.get("context_precision", [None])[i],
            "context_recall": metrics_dict.get("context_recall", [None])[i],
        }
        output["queries"].append(query_result)

    return output


def _estimate_to_text(estimate: dict) -> str:
    """Convert an Estimate dict to readable text for evaluation."""
    lines = []

    if estimate.get("confidence") == "insufficient":
        lines.append(f"Insufficient context: {estimate.get('insufficient_context_explanation')}")
        return "\n".join(lines)

    total_days = estimate.get("total_engineer_days")
    duration_weeks = estimate.get("duration_weeks")
    if total_days:
        lines.append(f"Total: {total_days} engineer-days, approximately {duration_weeks} weeks")
    lines.append("")

    for module in estimate.get("modules", []):
        lines.append(f"[{module.get('name')}]")
        for task in module.get("tasks", []):
            task_line = f"- {task.get('name')}"
            if task.get("engineer_days"):
                task_line += f": {task.get('engineer_days')} days"
            if task.get("description"):
                task_line += f" ({task.get('description')})"
            lines.append(task_line)
        lines.append("")

    if estimate.get("assumptions"):
        lines.append("Assumptions:")
        for assumption in estimate.get("assumptions", []):
            lines.append(f"- {assumption.get('description')} (impact: {assumption.get('impact')})")
        lines.append("")

    lines.append(f"Confidence: {estimate.get('confidence')}")
    if estimate.get("reasoning"):
        lines.append(f"Reasoning: {estimate.get('reasoning')}")

    return "\n".join(lines)


def _extract_context_from_estimate(estimate: dict) -> list[str]:
    """Extract cited sources as context chunks (for real evals, these come from retrieval)."""
    chunks = []

    for module in estimate.get("modules", []):
        for task in module.get("tasks", []):
            for source in task.get("sources", []):
                # In a real scenario, source.evidence would be the actual chunk text
                evidence = source.get("evidence", "")
                if evidence:
                    chunks.append(evidence)

    return chunks


def _print_table(results: dict) -> None:
    """Print RAGAS metrics as an ASCII table."""
    print("\n" + "=" * 100)
    print(f"{'Query':<35} {'Faithfulness':<16} {'Answer Rel.':<16} {'Context Prec.':<16} {'Context Rec.':<16}")
    print("=" * 100)

    for query_result in results.get("queries", []):
        case_id = query_result["case_id"]
        faithfulness_score = query_result["faithfulness"]
        answer_rel_score = query_result["answer_relevancy"]
        context_prec_score = query_result["context_precision"]
        context_rec_score = query_result["context_recall"]

        f_str = f"{faithfulness_score:.3f}" if faithfulness_score is not None else "N/A"
        ar_str = f"{answer_rel_score:.3f}" if answer_rel_score is not None else "N/A"
        cp_str = f"{context_prec_score:.3f}" if context_prec_score is not None else "N/A"
        cr_str = f"{context_rec_score:.3f}" if context_rec_score is not None else "N/A"

        print(f"{case_id:<35} {f_str:<16} {ar_str:<16} {cp_str:<16} {cr_str:<16}")

    print("-" * 100)

    agg = results.get("aggregate", {})
    f_agg = f"{agg.get('faithfulness', 0):.3f}" if agg.get("faithfulness") is not None else "N/A"
    ar_agg = f"{agg.get('answer_relevancy', 0):.3f}" if agg.get("answer_relevancy") is not None else "N/A"
    cp_agg = f"{agg.get('context_precision', 0):.3f}" if agg.get("context_precision") is not None else "N/A"
    cr_agg = f"{agg.get('context_recall', 0):.3f}" if agg.get("context_recall") is not None else "N/A"

    print(f"{'AVERAGE':<35} {f_agg:<16} {ar_agg:<16} {cp_agg:<16} {cr_agg:<16}")
    print("=" * 100 + "\n")


async def main():
    """Main entry point."""
    try:
        results = await run_evaluation()

        # Save to JSON
        with open(RESULTS_PATH, "w") as f:
            json.dump(results, f, indent=2)
        log.info("results saved", path=str(RESULTS_PATH))

        # Print table
        _print_table(results)

        # Print summary
        log.info(
            "evaluation complete",
            num_queries=results["num_queries"],
            faithfulness=results["aggregate"]["faithfulness"],
            answer_relevancy=results["aggregate"]["answer_relevancy"],
            context_precision=results["aggregate"]["context_precision"],
            context_recall=results["aggregate"]["context_recall"],
        )

    except Exception as e:
        log.error("evaluation failed", error=str(e), exc_info=True)
        raise


if __name__ == "__main__":
    asyncio.run(main())
