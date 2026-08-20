# syntax=docker/dockerfile:1
# check=error=true

# =============================================================================
# Business backend (Rails 8) — Session 15
# =============================================================================
# Until S14 this image carried ONLY the gems: the application source arrived
# through a compose bind mount, so the image was useless anywhere but this
# laptop. Session 15 makes it SELF-CONTAINED — the source is baked in and
# `docker run` alone boots a working app, which is the precondition for the
# cloud deploy in the live session.
#
# Session 15 (live) makes the image ENVIRONMENT-AWARE. It still defaults to
# RAILS_ENV=development, so the local compose flow is untouched, but building
# with `--build-arg RAILS_ENV=production` produces a real production image:
# gems without the development/test groups, assets precompiled, and static files
# served by the app. That is what the CI `build` job publishes to GHCR, and what
# docker-compose.prod.yml pulls onto the EC2 instance.
#
#     docker build --build-arg RAILS_ENV=production -t business-backend:prod .
#
# The four-database problem that used to block this is solved in
# config/database.yml: the `production:` block now points cache/queue/cable at
# the SAME DATABASE_URL as primary, because a managed platform hands you one
# database, not four. See docs/decisions.md.

ARG RUBY_VERSION=3.4.4
FROM docker.io/library/ruby:$RUBY_VERSION-slim

WORKDIR /rails

# Toolchain + runtime libraries (single layer, no recommends).
# - build-essential / git / pkg-config / libyaml-dev: native gem compilation
#   (bootsnap, debug, nokogiri).
# - libpq-dev / postgresql-client: pg driver + psql/pg_isready for debugging.
# - libvips / libjemalloc2: parity with what Rails-generated prod image carried.
# - curl: HEALTHCHECK and ad-hoc debugging.
RUN apt-get update -qq && \
    apt-get install --no-install-recommends -y \
        build-essential \
        curl \
        git \
        libjemalloc2 \
        libpq-dev \
        libvips \
        libyaml-dev \
        pkg-config \
        postgresql-client && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

# The build argument that switches the whole image. Declared AFTER the FROM so
# it is visible to the build stage, and promoted to an ENV so it also becomes
# the container's default runtime environment.
ARG RAILS_ENV=development

ENV BUNDLE_PATH=/usr/local/bundle \
    BUNDLE_JOBS=4 \
    BUNDLE_RETRY=3 \
    RAILS_ENV=${RAILS_ENV} \
    RAILS_LOG_TO_STDOUT=1

# Gems first, in their own layer: they only reinstall when the Gemfile changes,
# so editing application code does not pay for a bundle install.
#
# In production the development and test groups are skipped, which drops the
# debuggers, the test framework and the browser driver — a smaller image and a
# smaller attack surface. BUNDLE_DEPLOYMENT additionally refuses to run if
# Gemfile.lock is out of date, turning a silent drift into a build failure.
COPY Gemfile Gemfile.lock ./
RUN if [ "$RAILS_ENV" = "production" ]; then \
        bundle config set --local deployment true && \
        bundle config set --local without "development test"; \
    fi && \
    bundle install && \
    rm -rf "${BUNDLE_PATH}"/ruby/*/cache

# foreman is intentionally outside bundle (bin/dev convention).
RUN gem install foreman

# Application source. From here on, any code change invalidates the cache —
# which is why it comes AFTER the gem install.
COPY . .

# Build the Tailwind stylesheet at image-build time so the container does not
# depend on the `tailwindcss:watch` process to render correctly. In dev the
# override re-enables the watcher via bin/dev.
# SECRET_KEY_BASE_DUMMY lets the asset task boot the app without a real secret —
# assets must never need the real one, or it would have to live in the image.
RUN SECRET_KEY_BASE_DUMMY=1 bundle exec rails tailwindcss:build

# In production, also fingerprint and copy the assets into public/assets, and
# tell the app to serve them itself. Behind Render's edge there is no separate
# Caddy proxies to Puma without serving files itself, so without
# RAILS_SERVE_STATIC_FILES every CSS and JS request 404s and the app renders
# unstyled.
RUN if [ "$RAILS_ENV" = "production" ]; then \
        SECRET_KEY_BASE_DUMMY=1 bundle exec rails assets:precompile; \
    fi
# Set unconditionally: `ENV` cannot be conditional, and it is a no-op in
# development, where Rails serves public/ regardless. In production it is what
# makes the precompiled assets reachable.
ENV RAILS_SERVE_STATIC_FILES=true

# Run as a non-root user (parity with the ai-service image). Ownership covers
# the writable paths Rails needs at runtime: logs, tmp (pids, cache) and storage.
RUN groupadd --system --gid 1000 rails && \
    useradd --system --uid 1000 --gid 1000 --create-home --shell /bin/bash rails && \
    mkdir -p log tmp storage && \
    chown -R rails:rails /rails log tmp storage
USER 1000:1000

ENTRYPOINT ["/rails/bin/docker-entrypoint"]

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
    CMD curl -f http://localhost:3000/up || exit 1

CMD ["bin/rails", "server", "-b", "0.0.0.0", "-p", "3000"]
