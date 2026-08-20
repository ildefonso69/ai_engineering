#!/usr/bin/env bash
#
# Session 15 — restore the vector corpus into a deployed instance.
#
# Runs against the DATABASE, not against the HTTP API, and that is deliberate:
# the ingest endpoints require the X-Service-Token (they are not exempt), the
# ingest scripts do not send it, and going through the API would re-embed
# everything anyway. Restoring copies the vectors that are already paid for.
#
#     ./scripts/restore_corpus.sh /tmp/corpus.dump
#
# Idempotent enough to re-run: --clean drops the objects before recreating them.
# It does NOT touch the Rails database, only the vector store.

set -euo pipefail

DUMP="${1:?usage: restore_corpus.sh <file.dump>}"
COMPOSE_FILES="${COMPOSE_FILES:--f docker-compose.yml -f docker-compose.prod.yml}"
SERVICE="${SERVICE:-vector-db}"
DB_USER="${VECTOR_DB_USER:-estimator}"
DB_NAME="${VECTOR_DB_NAME:-estimator}"

[ -f "${DUMP}" ] || { echo "[restore] ERROR: no such file: ${DUMP}" >&2; exit 1; }

echo "[restore] ${DUMP} -> ${SERVICE} :: ${DB_NAME}"

# shellcheck disable=SC2086
if ! docker compose ${COMPOSE_FILES} ps --status running --services 2>/dev/null | grep -qx "${SERVICE}"; then
    echo "[restore] ERROR: the '${SERVICE}' service is not running." >&2
    exit 1
fi

# pgvector must exist BEFORE the restore: the dump contains columns of type
# `vector`, and Postgres cannot create them without the extension. On this
# deployment the extension normally arrives via alembic migration 0002, but the
# restore may run before the AI service has ever started.
echo "[restore] Ensuring the pgvector extension is present"
# shellcheck disable=SC2086
docker compose ${COMPOSE_FILES} exec -T "${SERVICE}" \
    psql -U "${DB_USER}" -d "${DB_NAME}" -c 'CREATE EXTENSION IF NOT EXISTS vector;'

echo "[restore] Restoring (this drops and recreates the existing objects)"
# --clean --if-exists: re-runnable.
# --no-owner: role names differ between environments.
# Exit status is checked loosely on purpose: pg_restore reports non-zero for
# benign notices (e.g. "extension already exists"), which would abort `set -e`.
# shellcheck disable=SC2086
if docker compose ${COMPOSE_FILES} exec -T "${SERVICE}" \
        pg_restore -U "${DB_USER}" -d "${DB_NAME}" --clean --if-exists --no-owner \
        < "${DUMP}"; then
    echo "[restore] pg_restore finished cleanly"
else
    echo "[restore] pg_restore reported warnings — verifying the result instead"
fi

echo
echo "[restore] Verification:"
# shellcheck disable=SC2086
docker compose ${COMPOSE_FILES} exec -T "${SERVICE}" \
    psql -U "${DB_USER}" -d "${DB_NAME}" -c \
    "SELECT 'documents' AS tabla, count(*) FROM documents
     UNION ALL SELECT 'budget_chunks', count(*) FROM budget_chunks;"

echo "[restore] Done. If the counts are zero, the dump was empty or restored elsewhere."
