# syntax=docker/dockerfile:1
# The AeroChorus app image: control plane API, migrations, review edge with the built
# web UI, and the CLI. docker-compose.yml builds it. (The worker runs natively; CrispASR
# comes from its own pinned image.)

ARG AEROCHORUS_REVISION=unknown

# --- web UI -------------------------------------------------------------------------------
FROM node:24-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# --- app (default target) ---------------------------------------------------------------
FROM python:3.12-slim AS app
ARG AEROCHORUS_REVISION
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    AEROCHORUS_UI_DIST=/app/ui/dist \
    AEROCHORUS_WORKER_CONFIG=/etc/aerochorus/worker.toml
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev
COPY --from=ui /ui/dist /app/ui/dist
LABEL org.opencontainers.image.title="aerochorus-app" \
      org.opencontainers.image.revision="$AEROCHORUS_REVISION"
EXPOSE 8000 8080
CMD ["aerochorus", "api", "serve", "--host", "0.0.0.0", "--port", "8000"]
