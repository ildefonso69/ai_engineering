# estimator-web

Rails 8 frontend + business backend for the AI-engineering monorepo. Consumes the FastAPI service in `../estimator/` for LLM estimations.

Organized by **contexts mirroring the Master's modules** — `estimation` (S04, transactional), `conversation` (S05, multi-turn + ACB) and `rag` (S07, Chunking Lab) — over an `EstimatorAi` foundation that is the only layer talking HTTP to the service. Full rules and layer map in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Secciones de la UI

- **`/`** — home dashboard: un card por contexto del máster.
- **`/estimations`** — estimación transaccional (S04).
- **`/chat_sessions`** — conversación multi-turno con adjuntos, tiers y modo Actor-Critic-Boss (S05).
- **`/ai_settings`** — **Ajustes**: cambia los modelos LLM del servicio (primario, fallback, critic, metadata, compression y chunkers) **en caliente** vía `PUT /api/v1/config/models` — sin editar `.env` ni recrear contenedores. El modelo primario activo se muestra siempre como badge en la navbar (oculto si el servicio está caído). «Por defecto» restaura el valor del `.env`.
- **`/rag/chunking_comparisons`** — **Chunking Lab** (S07): comparativa de las 8 estrategias de chunking sobre el corpus de 17 presupuestos (`lib/estimator_ai/data/`), con estadísticas por estrategia (tokens, huérfanos/obesos, coste, segundos), barras de coste y playground de retrieval (top-k por similitud coseno, badges parent/child en `hierarchical`). Cada run se persiste (`chunking_comparisons`, payload JSONB) para revisitar resultados caros (contextual_retrieval ≈ $0.14 / 3 min) sin re-pagar. Las estrategias de pago son opt-in y van avisadas en el formulario; la llamada usa un timeout propio de 600 s.

## Stack

- Ruby 3.4.4 / Rails 8.0.5
- PostgreSQL 16 (containerized)
- Tailwind CSS 4 via `tailwindcss-rails` (standalone binary, no Node required)
- Hotwire (Turbo + Stimulus) + Importmap + Propshaft
- Solid Cache / Queue / Cable — all in-memory in development, no Redis

## Quick start (Docker, recommended)

```bash
cp .env.example .env
docker compose up --build
```

App at http://localhost:3000. Healthcheck at `/up`.

The first build compiles native gems (`bootsnap`, `nokogiri`, `pg`, `debug`) and may take several minutes; subsequent builds are cached unless `Gemfile.lock` changes.

## Quick start (local, no Docker)

Requires Postgres listening on the local Unix socket.

```bash
bin/setup        # bundle install + db:prepare
bin/dev          # Puma + tailwindcss:watch via foreman, port 3000
```

`config/database.yml` reads `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_USER`, `DATABASE_PASSWORD` from ENV. When unset, Rails falls back to the Unix socket — that is the local-without-docker path.

## Common commands

```bash
docker compose exec estimator-web bin/rails console
docker compose exec estimator-web bin/rails db:migrate
docker compose exec estimator-web bin/rails test
docker compose exec estimator-web bash

# Connect to the dev DB with psql
docker compose exec postgres psql -U postgres estimator_web_development

# Live logs
docker compose logs -f estimator-web

# Add a gem (rebuild to bake it into the image)
docker compose exec estimator-web bundle add <gem>
docker compose build estimator-web
```

## Cross-service calls

When this project is launched from the **monorepo root** (`docker compose up` in `../`), it shares a network with the FastAPI estimator. Rails can reach it at `http://estimator:8000` (see `ESTIMATOR_API_BASE_URL` in `.env.example`). Launching `estimator-web` standalone leaves that hostname unresolvable — by design.

## Production / Kamal

Out of scope. The `Dockerfile` here is development-only. The `kamal` and `thruster` gems and the `.kamal/` + `config/deploy.yml` files are leftovers from `rails new` and currently unused; they do not load at runtime (`require: false`) and can be removed if production stays off the table.
