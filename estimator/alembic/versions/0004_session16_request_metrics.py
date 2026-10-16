"""Session 16 — request_metrics table (production observability golden set).

Revision ID: 0004_session16_request_metrics
Revises: 0003_session10_fts
Create Date: 2026-10-16 00:00:00

Durable store for the 3 production signals the exercise asks for (latency,
cost, error rate). Structured logs (structlog, console-only Logfire spans per
Session 13's decision) already carry this data, but container stdout is
ephemeral and not something a dashboard should tail — this table is the
queryable surface the dashboard reads instead. Plain, flat columns (no FK,
no JSONB) on purpose: this is an append-only metrics feed, not a relational
entity, so there is nothing to normalize.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_session16_request_metrics"
down_revision: Union[str, None] = "0003_session10_fts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "request_metrics",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("endpoint", sa.String(length=100), nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=False),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Float, nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False),
    )
    op.create_index("ix_request_metrics_recorded_at", "request_metrics", ["recorded_at"])
    op.create_index("ix_request_metrics_endpoint", "request_metrics", ["endpoint"])


def downgrade() -> None:
    op.drop_index("ix_request_metrics_endpoint", table_name="request_metrics")
    op.drop_index("ix_request_metrics_recorded_at", table_name="request_metrics")
    op.drop_table("request_metrics")
