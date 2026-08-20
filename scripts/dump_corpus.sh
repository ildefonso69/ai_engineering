#!/usr/bin/env bash
#
# Session 15 — export the vector corpus so a fresh deployment does not have to
# re-embed it.
#
# WHY THIS EXISTS. A new EC2 instance comes up with an empty pgvector volume.
# Re-ingesting the corpus means re-embedding ~1.5k task chunks plus the budgets,
# transcripts and technical docs — real money, several minutes, and an
# OPENAI_API_KEY on the box. The embeddings are already paid for; carrying them
# across costs nothing.
#
# It dumps the WHOLE database (schema + data + the alembic version table), so
# the restored instance also knows which migrations are applied.
#
#     ./scripts/dump_corpus.sh                    # -> backups/corpus-<date>.dump
#     ./scripts/dump_corpus.sh /tmp/corpus.dump
#
# The dump is a custom-format (-Fc) file: compressed and restorable in parallel.

set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
SERVICE="${SERVICE:-vector-db}"
DB_USER="${VECTOR_DB_USER:-estimator}"
DB_NAME="${VECTOR_DB_NAME:-estimator}"

OUT="${1:-backups/corpus-$(date +%Y%m%d-%H%M%S).dump}"
mkdir -p "$(dirname "$OUT")"

echo "[dump] ${SERVICE} :: ${DB_NAME} -> ${OUT}"

if ! docker compose -f "${COMPOSE_FILE}" ps --status running --services 2>/dev/null | grep -qx "${SERVICE}"; then
    echo "[dump] ERROR: the '${SERVICE}' service is not running." >&2
    echo "[dump] Start it first:  docker compose up -d ${SERVICE}" >&2
    exit 1
fi

# -Fc  custom format (compressed, restorable selectively)
# --no-owner / --no-privileges: the role names differ between environments, and
# a dump that insists on its original owner fails to restore anywhere else.
docker compose -f "${COMPOSE_FILE}" exec -T "${SERVICE}" \
    pg_dump -U "${DB_USER}" -d "${DB_NAME}" -Fc --no-owner --no-privileges \
    > "${OUT}"

SIZE=$(du -h "${OUT}" | cut -f1)
echo "[dump] Done: ${OUT} (${SIZE})"
echo
echo "Copy it to the instance and restore:"
echo "    scp ${OUT} ubuntu@\$EC2_HOST:/tmp/corpus.dump"
echo "    ssh ubuntu@\$EC2_HOST 'cd /opt/estimator && ./scripts/restore_corpus.sh /tmp/corpus.dump'"
