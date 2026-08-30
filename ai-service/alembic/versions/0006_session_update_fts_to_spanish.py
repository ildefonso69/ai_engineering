"""Update full-text search configuration to Spanish.

Revision ID: 0006_session_update_fts_to_spanish
Revises: 0005_session11_hnsw_multi_index
Create Date: 2026-08-30 00:00:00

Updates the STORED generated ``content_tsv`` column on all chunk tables
(``budget_chunks``, ``transcript_chunks``, ``technical_doc_chunks``) to use
the Spanish text-search configuration instead of English.

The corpus contains Spanish-language budgets and requires Spanish stemming
and stop-word lists for accurate full-text search. Migration 0003 used the
``english`` configuration, which is now replaced with ``spanish`` to match
the corpus language.

The ``content_tsv`` column remains GENERATED ALWAYS … STORED: Postgres
automatically recomputes the tsvector on every insert/update of ``content``
using the new configuration. The GIN index is recreated to apply the new
regconfig to all existing rows.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0006_fts_spanish"
down_revision: Union[str, None] = "0005_session11_hnsw_multi_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHUNK_TABLES = ("budget_chunks", "transcript_chunks", "technical_doc_chunks")
FTS_REGCONFIG_OLD = "english"
FTS_REGCONFIG_NEW = "spanish"


def _gin_index_name(table: str) -> str:
    return f"ix_{table}_content_tsv"


def upgrade() -> None:
    for table in CHUNK_TABLES:
        gin_index = _gin_index_name(table)

        # Drop the existing GIN index built with the old regconfig.
        op.execute(f"DROP INDEX IF EXISTS {gin_index}")

        # Drop the generated column; Postgres will remove the underlying computed expression.
        op.execute(f"ALTER TABLE {table} DROP COLUMN content_tsv")

        # Recreate the column with the new Spanish configuration.
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN content_tsv tsvector "
            f"GENERATED ALWAYS AS (to_tsvector('{FTS_REGCONFIG_NEW}', content)) STORED"
        )

        # Recreate the GIN index on the updated column.
        op.create_index(
            gin_index,
            table,
            ["content_tsv"],
            postgresql_using="gin",
        )


def downgrade() -> None:
    for table in CHUNK_TABLES:
        gin_index = _gin_index_name(table)

        # Reverse: drop the Spanish-configured column and GIN index.
        op.execute(f"DROP INDEX IF EXISTS {gin_index}")
        op.execute(f"ALTER TABLE {table} DROP COLUMN content_tsv")

        # Restore the English configuration.
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN content_tsv tsvector "
            f"GENERATED ALWAYS AS (to_tsvector('{FTS_REGCONFIG_OLD}', content)) STORED"
        )

        # Recreate the GIN index with the old regconfig.
        op.create_index(
            gin_index,
            table,
            ["content_tsv"],
            postgresql_using="gin",
        )
