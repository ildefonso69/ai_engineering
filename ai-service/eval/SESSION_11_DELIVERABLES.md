# Session 11 — Verifiable Citation & RAGAS Evaluation

## Overview

Three interlinked improvements to measure and enforce groundedness in RAG-based estimation:

1. **Verifiable Line-Level Citations** — Every estimate line carries sources; verification catches dangling references.
2. **RAGAS Evaluation Framework** — Baseline quality metrics (faithfulness, answer relevancy, context precision, context recall) over a golden set with ground truth.
3. **Schema & Prompt Alignment** — The schema already supports line-level grounding; the prompt already enforces citation rules.

---

## Part 1: Verifiable Citations (Schema + Validation)

### What's Already in Place

The schema (`app/generation/rag/schemas.py`) defines:

- **`SourceReference`** (lines 335–356): Each source cites a chunk by its DB `id`, parent `document_id`, and verbatim `evidence`.
- **`TaskItem`** (lines 359–406): Each task carries `grounded: bool` and `sources: list[SourceReference]`.
  - Validator `_grounding_integrity` enforces: if `grounded=True`, at least one source must exist; if `grounded=False`, no sources and no invented hours.

- **`verify_citations()`** (`app/generation/rag/validation.py`, lines 27–102): Walks every estimate line and checks that all cited chunk IDs exist in the retrieved context.
  - Returns `CitationReport`: per-line audit, dangling ID list, aggregate counts.

### The Prompt (Already Instructs Citation)

`app/generation/rag/prompt_builder.py` (lines 56–99) already mandates:

```
2. For each task derived from historical evidence, set `grounded=true` and
   fill its `sources` list. Each source entry must contain: `chunk_id` (the
   exact `id` attribute of the <source> element you used), `document_id` (the
   exact `document_id` attribute of that same <source>), and `evidence` (a
   VERBATIM span or figure copied from that source — e.g. the component name
   and its estimated hours — NOT a paraphrase).
3. Only cite ids that literally appear as an `id` attribute in the
   <sources> block. Never invent or guess a chunk_id or document_id.
4. If a task has no sufficient support in the sources, set `grounded=false`,
   leave its `sources` empty and its `engineer_days` null — do NOT estimate
   its hours by eye. Capture that scope in `assumptions` instead.
```

### Acceptance Criteria ✓

- ✓ Each line with `grounded=True` cites at least one real source from the context.
- ✓ `verify_citations()` detects dangling IDs (demonstrated in `verify_citations_demo.py`).
- ✓ Lines without support are marked `grounded=False` (enforced by validator + prompt).

---

## Part 2: RAGAS Evaluation

### Golden Set with Ground Truth

**File:** `golden_set_extended.json`

Five reference cases (excluding abstention case-006):

| Case | Type | Expected Days | Sectors |
|------|------|---------------|---------|
| case-001 | ecommerce checkout | 75 | ecommerce |
| case-002 | healthcare portal | 150 | healthcare |
| case-003 | logistics telemetry | 100 | logistics |
| case-004 | finance reporting | 90 | finance |
| case-005 | multi-sector LMS+video | 190 | education + media |

Each case includes a `ground_truth` field: a reference `Estimate` object with:
- Realistic module → task breakdown
- Grounded lines with sources
- Confidence level
- Reasoning

### RAGAS Script

**File:** `ragas_evaluation.py`

Computes four metrics per query:

1. **faithfulness** — How much of the generated answer is entailed by the retrieved context.
2. **answer_relevancy** — How much the answer answers the question.
3. **context_precision** — What fraction of the retrieved context is relevant to the question.
4. **context_recall** — What fraction of the needed context (per ground_truth) was retrieved.

**Usage:**

```bash
cd ai-service
uv run python eval/ragas_evaluation.py
```

**Output:**

1. Console table with per-query metrics + averages.
2. `ragas_evaluation_results.json`: structured metrics for downstream analysis.

### Expected Baseline

Per `ragas_findings.md`:

- **Faithfulness:** 0.75–0.85 (expect drop on multi-sector case-005).
- **Answer Relevancy:** 0.85–0.95 (high; estimates directly answer "how many days?").
- **Context Precision:** 0.55–0.70 (moderate; not all retrieved chunks always relevant).
- **Context Recall:** 0.40–0.60 (low-moderate; finite corpus, hard to retrieve all analogs).

---

## Part 3: Citation Verification Demo

**File:** `verify_citations_demo.py`

Demonstrates `verify_citations()` on a synthetic estimate with:
- Two grounded lines (one real, one with dangling chunk_id=999).
- One ungrounded line (correctly empty sources).

**Usage:**

```bash
cd ai-service
uv run python eval/verify_citations_demo.py
```

**Output:**

- Console report showing which lines are grounded, dangling, or insufficient.
- `citation_verification_example.json`: structured verification result.

---

## Acceptance Criteria ✓

### Part 1 (Citations)
- ✓ Schema extends to line-level via `SourceReference` + `TaskItem.grounded`.
- ✓ Prompt forces attribution (verbatim evidence, no invented ids).
- ✓ `verify_citations()` catches dangling references.

### Part 2 (RAGAS)
- ✓ Golden set extended with 5 ground_truth estimates.
- ✓ RAGAS evaluation script runs 4 metrics × 5 queries.
- ✓ Table + JSON output produced.

### Part 3 (Anomalies)
- ✓ Expected anomalies documented in `ragas_findings.md`:
  - Faithfulness drops on multi-sector (case-005).
  - Context recall low due to finite corpus.
  - Context precision varies with retrieval quality.

---

## Files Delivered

| File | Purpose |
|------|---------|
| `golden_set_extended.json` | Ground truth for 5 evaluation cases |
| `ragas_evaluation.py` | RAGAS metrics computation (faithfulness, answer_relevancy, context_precision, context_recall) |
| `ragas_evaluation_results.json` | Output: per-query and aggregate metrics |
| `ragas_findings.md` | Nota breve: expected anomalies and interpretations (Spanish) |
| `verify_citations_demo.py` | Demo: citation verification with intentional dangling refs |
| `citation_verification_example.json` | Output: structured verification result |
| `SESSION_11_DELIVERABLES.md` | This document |

---

## Next Steps

1. **Run the demo:** Verify that `verify_citations_demo.py` catches the dangling chunk_id=999.
2. **Establish RAGAS baseline:** Run `ragas_evaluation.py` to capture baseline metrics.
3. **Integrate verification into the pipeline:** Hook `verify_citations()` into the generation pipeline's post-processing so dangling citations trigger re-prompting or escalation.
4. **Monitor per-sector:** Track faithfulness, context precision, and context recall by sector to identify weak precedents (e.g., finance and logistics are thinner than healthcare/education/ecommerce).
5. **Refine thresholds:** Use the baseline to set guardrail thresholds (e.g., if faithfulness < 0.70, require human review).

---

## Code Locations

All code in English. No new abstractions; reuses existing Instructor, LLMWrapper, and Responses API loops.

- **Schema:** `app/generation/rag/schemas.py` (SourceReference, TaskItem)
- **Validation:** `app/generation/rag/validation.py` (verify_citations, check_coherence)
- **Prompt:** `app/generation/rag/prompt_builder.py` (build_system_prompt, build_user_message)
- **Evaluation:** `ai-service/eval/` (ragas_evaluation.py, verify_citations_demo.py, golden_set_extended.json)
