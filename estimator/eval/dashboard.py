"""Self-contained production metrics dashboard (Session 16, Paso 4).

Reads the ``request_metrics`` table (populated by
``app/foundation/observability/metrics.py`` on every real call to
``POST /v1/estimate/from-transcript``) directly from Postgres and renders the
3 required production signals:

- Latency per request (mean, p95)
- Cost per request (derived from token counts)
- Error rate (fraction of non-"success" requests)

Deliberately NOT Logfire Cloud (Session 13's decision, reaffirmed here): this
is a self-contained panel with no external SaaS dependency and no new
account/token. Built with Streamlit because it is already a project
dependency (see ``streamlit_app.py``) — no new heavy dependency added.

Run with:

    cd estimator
    uv run streamlit run eval/dashboard.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import text

# Allow running as `uv run streamlit run eval/dashboard.py` from estimator/
# without installing the package — mirrors the sys.path handling other
# scripts/ entrypoints in this repo need when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.foundation.persistence.database import create_engine_from_settings  # noqa: E402

st.set_page_config(page_title="Estimator — Production Metrics", page_icon="📈")
st.title("Estimator — Production Metrics")
st.caption(
    "Golden-set-adjacent production dashboard (Session 16). Reads "
    "`request_metrics`, populated on every real call to "
    "`POST /v1/estimate/from-transcript`."
)


@st.cache_data(ttl=10)
def load_metrics(limit: int = 500) -> pd.DataFrame:
    engine = create_engine_from_settings()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT recorded_at, request_id, endpoint, latency_ms, prompt_tokens, "
                "completion_tokens, estimated_cost_usd, status "
                "FROM request_metrics ORDER BY recorded_at DESC LIMIT :limit"
            ),
            {"limit": limit},
        ).fetchall()
    return pd.DataFrame(
        rows,
        columns=[
            "recorded_at",
            "request_id",
            "endpoint",
            "latency_ms",
            "prompt_tokens",
            "completion_tokens",
            "estimated_cost_usd",
            "status",
        ],
    )


df = load_metrics()

if df.empty:
    st.warning(
        "No rows in `request_metrics` yet. Call `POST /v1/estimate/from-transcript` "
        "at least once (e.g. via `uv run python eval/run_eval.py`) to populate it."
    )
    st.stop()

col1, col2, col3 = st.columns(3)

mean_latency = df["latency_ms"].mean()
p95_latency = df["latency_ms"].quantile(0.95)
col1.metric("Mean latency", f"{mean_latency:,.0f} ms")
col1.metric("p95 latency", f"{p95_latency:,.0f} ms")

mean_cost = df["estimated_cost_usd"].mean()
total_cost = df["estimated_cost_usd"].sum()
col2.metric("Mean cost / request", f"${mean_cost:.4f}")
col2.metric("Total cost (this window)", f"${total_cost:.2f}")

error_rate = (df["status"] != "success").mean()
col3.metric("Error rate", f"{error_rate:.1%}")
col3.metric("Requests (window)", f"{len(df)}")

st.subheader("Latency over time")
st.line_chart(df.set_index("recorded_at")["latency_ms"].iloc[::-1])

st.subheader("Cost over time")
st.line_chart(df.set_index("recorded_at")["estimated_cost_usd"].iloc[::-1])

st.subheader("Recent requests")
st.dataframe(df, use_container_width=True)

st.caption(
    "'Good' for this system: compare against the golden set's own pass bar "
    "(`eval/golden_set.json` + `eval/run_eval.py`) — mean_latency_ms and "
    "within_range_rate there are the quality floor this dashboard should stay "
    "consistent with. See `docs/evaluation.md`."
)
