# Local deployment (Session 15)

Containerized, single-command startup for Project 2's three layers: business
backend (`estimator-web`, Rails — the only public entry point), AI service
(`estimator`, FastAPI — internal-only, never published to the host), relational
database (`postgres`, inside `estimator-web`), and vector database
(`estimator-postgres`, pgvector, inside `estimator`).

## Prerequisites

- Docker and Docker Compose installed (`docker --version`, `docker compose version` — this setup uses the `!override` Compose merge tag, which requires **Compose v2.24+**).
- A copy of `.env` at the repo root, based on `.env.example` (see below), with a real LLM API key. Never commit `.env`.

## Starting the stack

From the repository root (not from `estimator/` or `estimator-web/` — that launches a separate, isolated Compose project with its own volumes, per the note at the top of the root `docker-compose.yml`):

```bash
cp .env.example .env      # fill in real values, never commit this file
docker compose build
docker compose up -d
```

Bringing it back down and up again should require no manual steps — persistent
data (the seeded corpus, uploaded documents, Rails records) survives via
Docker named volumes:

```bash
docker compose down
docker compose up -d
```

## The 5 checks

**1. All four services are up, and the two that matter for readiness are `healthy`.**

```bash
docker compose ps
```

Expect `estimator`, `estimator-postgres`, `estimator-redis`, `estimator-web`
and `estimator-web`'s `postgres` all `Up`, with `estimator` and both Postgres
containers reporting `(healthy)`.

**2. The business backend is reachable from the host.**

```bash
curl -i http://localhost:3000/
```

Expect a `200` (or a redirect into the app) — this is the one and only public
entry point.

**3. The AI service is NOT reachable from the host.**

```bash
curl -m 5 http://localhost:8000/health
```

Expect a connection failure (curl exit code `7`, "connection refused"), not a
response. `estimator` only publishes port 8000 when run **standalone**
(`cd estimator && docker compose up`, for direct `curl`/httpie use during
live sessions) — under the root compose project it carries
`ports: !override []`, which replaces the inherited port list instead of
appending to it. Verify the merged config directly without touching Docker:

```bash
docker compose config | grep -A3 "^  estimator:"
# no "ports:" key should appear for the estimator service
# (scripts/check-estimator-not-exposed.sh automates this exact check)
```

**4. An estimation submitted through the business backend runs the full
internal chain and returns a coherent result.**

Submit an estimate through `estimator-web`'s UI (`http://localhost:3000`).
Internally this exercises: `estimator-web` → `estimator` (over the compose
network, authenticated with the `X-Service-Token` header /
`AI_SERVICE_TOKEN`) → the vector database (`estimator-postgres`) → back to
`estimator-web` → rendered response. A request to `estimator` missing or
carrying the wrong token gets `401`; the correct token gets `200`.

**5. Data survives a full restart.**

```bash
docker compose down
docker compose up -d
```

Confirm the same estimate/history is still visible in the business backend's
UI, and (if you have `psql` access) that `estimator-postgres`'s seeded corpus
row count is unchanged — proving persistence is volume-backed, not
container-lifetime-backed.

## Notes

- `AI_SERVICE_TOKEN` unset (not present in `.env` at all) leaves
  `/api/v1/estimate` open — intentional, for backward compatibility with
  existing unauthenticated tests/demos run against `estimator` standalone.
  Setting it to an **empty string** is normalized to the same "unset" state
  (a real bug found and fixed during this session — Compose substitutes an
  empty string, not an absent variable, when `.env` doesn't define it; the
  fix lives in `estimator/app/config.py`'s `AI_SERVICE_TOKEN` field
  validator). Under the root compose project, always set a real
  `AI_SERVICE_TOKEN` so the internal-network boundary has two independent
  controls (no published port + a checked token), not just one.
- Never commit `.env`. `.env.example` (root) and `estimator/.env.example`
  document every variable with no real values.
