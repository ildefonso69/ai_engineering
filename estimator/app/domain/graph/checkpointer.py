"""Postgres checkpointer wiring for the estimation graph (Level 2).

The graph persists its state per ``thread_id`` in the SAME project Postgres that
holds the pgvector embeddings — the checkpointer creates its own tables
(``checkpoints``, ``checkpoint_writes``, ``checkpoint_blobs``) and coexists with
them. No new infrastructure.

LangGraph's ``AsyncPostgresSaver`` is built on **psycopg3 (async)**, so it wants a
plain libpq DSN (``postgresql://user:pass@host/db``) — NOT the SQLAlchemy
``postgresql+psycopg://`` / ``+asyncpg`` forms. ``saver_conninfo`` derives it from
the single ``DATABASE_URL`` by stripping the driver token, mirroring
``_async_database_url`` in ``app/foundation/persistence/database.py`` (which swaps
the token for the SQLAlchemy async engine instead).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.config import Settings, get_settings

log = structlog.get_logger()


def saver_conninfo(settings: Settings | None = None) -> str:
    """Return a plain libpq DSN for ``AsyncPostgresSaver`` from ``DATABASE_URL``.

    ``postgresql+psycopg://…`` / ``postgresql+asyncpg://…`` → ``postgresql://…``.
    """
    url = (settings or get_settings()).DATABASE_URL
    if "+psycopg" in url:
        return url.replace("+psycopg", "")
    if "+asyncpg" in url:
        return url.replace("+asyncpg", "")
    return url


@asynccontextmanager
async def open_checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    """Open an ``AsyncPostgresSaver`` over the project Postgres and set it up.

    ``setup()`` is idempotent — it creates the checkpointer tables on first run and
    is a no-op afterwards — so calling it on every startup is safe. Use as an async
    context manager (e.g. entered into the app's ``AsyncExitStack`` in ``lifespan``)
    so the underlying connection is closed on shutdown.
    """
    conninfo = saver_conninfo()
    async with AsyncPostgresSaver.from_conn_string(conninfo) as checkpointer:
        await checkpointer.setup()
        log.info("graph_checkpointer_ready")
        yield checkpointer
