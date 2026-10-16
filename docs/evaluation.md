# Evaluation & production metrics (Session 16)

Two independent but related pieces: an offline **golden-set evaluation** of
estimate quality, and a live **production metrics dashboard**. The golden
set tells you if the model is still good; the dashboard tells you how the
service is actually behaving in traffic. Run the golden set after any prompt,
model, or retrieval change; watch the dashboard continuously.

All paths below (`eval/...`, `alembic/versions/...`, `app/foundation/...`)
are relative to `estimator/`, not to the repo root.

## Golden-set evaluation

- Cases: `eval/golden_set.json` (5 cases — estimation + one abstention case).
- Harness: `eval/run_eval.py`. Calls the REAL, running
  `POST /v1/estimate/from-transcript` endpoint — no mocking, and no
  `idempotency_key` — every run genuinely re-executes the pipeline (real
  LLM cost, deliberately) instead of risking a stale cache hit from a
  previous run.

```bash
cd estimator
ESTIMATE_API_KEY=<your key>  uv run python eval/run_eval.py
# or point at a different deployment:
uv run python eval/run_eval.py --base-url http://localhost:8000 --api-key <key>
```

Reports land in two places:
- `eval/last_run_report.json` — always-overwritten convenience copy of the
  latest run.
- `eval/reports/report_<timestamp>.json` — append-only historical series, so
  a later run can be diffed against past ones to catch quality regressions.

Headline metrics (see `run_eval.py::aggregate` for the full breakdown):
`within_range_rate`, `mean_absolute_error` (vs. the acceptable range's
midpoint — this golden set uses ranges, not point values), `abstention_correct`,
`mean_latency_ms`, plus per-category pass rate and citation recall.

**This sandbox could not run the harness** — it needs the real
`ESTIMATE_API_KEY`, which this environment blocks extracting
("credential materialization"). Run it yourself once with the real key.

## Production metrics dashboard

Every real call to `POST /v1/estimate/from-transcript` now writes a row to
Postgres (`request_metrics`, see `alembic/versions/0004_session16_request_metrics.py`)
via `app/foundation/observability/metrics.py::record_request_metrics`, for
both success AND failure paths. It also logs a `estimate_request_metrics`
structlog event, visible in the normal console log stream / Logfire console
spans (same no-token, console-only setup as Session 13 — no Logfire Cloud).

Token counts come from `LLMWrapper.complete_structured`
(`app/foundation/llm/wrapper.py`), which now uses Instructor's
`create_with_completion` instead of plain `create` — the raw completion's
`.usage.prompt_tokens` / `.usage.completion_tokens` were already there,
just discarded by the old `create()` call. Cost is derived from those tokens
via the wrapper's existing `MODEL_COSTS` table (approximate, illustrative —
provider pricing drifts).

Only `/v1/estimate/from-transcript` is wired (the primary production path
the golden set also targets). The other three estimate variants
(`-agentic`, `-graph`, `-multiagent`) are NOT instrumented yet — they use
different generation primitives (manual agent loop / LangGraph nodes) and
wiring them is a separate, larger change; flagged here rather than silently
skipped.

Run the dashboard:

```bash
cd estimator
uv run streamlit run eval/dashboard.py
```

It reads `request_metrics` directly (same `DATABASE_URL` as the service) and
renders:
- **Latency per request** — mean and p95 (ms).
- **Cost per request** — mean and running total (USD, approximate).
- **Error rate** — fraction of rows where `status != "success"`.

plus a time series for latency/cost and a raw table of the last 500 rows.

**"Good" for this system**: use the golden set's own bar as the quality
floor — `mean_latency_ms` from the last golden-set run is the latency this
dashboard's mean should track, and a rising error rate or a latency mean far
above that figure is the signal to re-run the golden set and check for
regressions before touching anything else.

This sandbox could not generate real traffic either (same credential block),
so the dashboard has not been run against live data or screenshotted here.
Once you've run the golden set above (which calls the real endpoint 5
times), `request_metrics` will have rows and the dashboard will render them.
