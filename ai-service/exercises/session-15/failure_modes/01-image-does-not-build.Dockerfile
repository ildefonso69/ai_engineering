# FAILURE MODE 1 — the image does not build.
#
# Symptom (verbatim, and the most common Docker error there is):
#   ERROR: failed to solve: failed to compute cache key: failed to calculate
#   checksum of ref ...: "/ai-service/pyproject.toml": not found
#
# Cause: THE BUILD CONTEXT, which is the monorepo trap. This Dockerfile writes
# its paths as if the context were the repository root:
#
#     COPY ai-service/pyproject.toml ./
#
# ...but compose builds it with `build: ./ai-service`, so the context IS
# ai-service/ and the path resolves to ai-service/ai-service/pyproject.toml,
# which does not exist. Docker cannot see anything outside the context, so no
# `../` escape hatch exists either.
#
# Two ways out, and the repo picks the first:
#   a) Context = the service directory, paths relative to it  (build: ./ai-service)
#   b) Context = the repo root, every path prefixed           (context: . +
#      dockerfile: ai-service/Dockerfile) — needed only when the image must
#      reach files shared between projects.
#
# Second, quieter defect: `COPY . .` sits BEFORE the dependency install, so
# every source edit invalidates the layer and reinstalls torch. This one still
# "builds" — it is just unusably slow, which is how it survives to production.
#
# Reproduce (from ai-service/):
#   docker build -f exercises/session-15/failure_modes/01-image-does-not-build.Dockerfile .
#
# Compare with the real ai-service/Dockerfile.
FROM python:3.11-slim

WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# BROKEN: source copied first...
COPY . .
# BROKEN: ...and paths written for the wrong context
COPY ai-service/pyproject.toml ai-service/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
