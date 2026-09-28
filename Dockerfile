# syntax=docker/dockerfile:1
# AeroChorus images (ADR-023). Targets:
#   app     (default) control plane API, migrations, review edge with the built web UI,
#           adjudication runner. The dev Compose file builds this one.
#   worker  the transcription worker with the pinned CUDA 12 CrispASR build (Pascal-
#           capable, ADR-021), ffmpeg, and the same CLI. Runs on the Linux GPU host.
# deploy/linux/package.ps1 builds both and bundles them for the Linux host.

ARG AEROCHORUS_REVISION=unknown

# --- web UI -------------------------------------------------------------------------------
FROM node:24-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# --- pinned CrispASR (CUDA 12 build; never CUDA 13 on Pascal) -----------------------------
FROM ubuntu:24.04 AS crispasr
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/linux/crispasr.lock /tmp/crispasr.lock
RUN set -eu; . /tmp/crispasr.lock; \
    case "$CRISPASR_ASSET" in *cuda13*) echo "CUDA 13 asset refused (Pascal)"; exit 1;; esac; \
    curl -fL --retry 3 -o /tmp/crispasr.tgz "$CRISPASR_URL"; \
    echo "$CRISPASR_SHA256  /tmp/crispasr.tgz" | sha256sum -c -; \
    dest="/opt/crispasr/$CRISPASR_VERSION"; mkdir -p "$dest"; \
    tar -xzf /tmp/crispasr.tgz -C "$dest"; rm /tmp/crispasr.tgz; \
    bin="$(find "$dest" -type f -name crispasr -perm -u+x | head -1)"; test -n "$bin"; \
    ln -s "$bin" /opt/crispasr/crispasr; \
    printf '{"version": "%s", "commit": "%s", "asset": "%s", "asset_sha256": "%s", "binary": "%s", "binary_sha256": "%s"}\n' \
      "$CRISPASR_VERSION" "$CRISPASR_COMMIT" "$CRISPASR_ASSET" "$CRISPASR_SHA256" "$bin" \
      "$(sha256sum "$bin" | cut -d' ' -f1)" > /opt/crispasr/INSTALLED.json; \
    cat /opt/crispasr/INSTALLED.json

# --- worker (Linux GPU host) -------------------------------------------------------------
# CrispASR's CUDA 12 build needs libcudart.so.12 (in the CUDA base image) and cuBLAS 12;
# it bundles its own OpenBLAS/OpenMP. Only the two cuBLAS libraries are taken from the
# full runtime image, which saves about 2 GB. CUDA 12.8 needs driver >= 570; Pascal is
# supported through the 580 branch.
FROM nvidia/cuda:12.8.1-runtime-ubuntu24.04 AS cuda-runtime

FROM nvidia/cuda:12.8.1-base-ubuntu24.04 AS worker
ARG AEROCHORUS_REVISION
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3.12 ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=cuda-runtime /usr/local/cuda/lib64/libcublas.so.12* /usr/local/cuda/lib64/libcublasLt.so.12* /usr/local/cuda/lib64/
RUN ldconfig
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    AEROCHORUS_WORKER_CONFIG=/etc/aerochorus/worker.toml
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev
COPY --from=crispasr /opt/crispasr /opt/crispasr
LABEL org.opencontainers.image.title="aerochorus-worker" \
      org.opencontainers.image.revision="$AEROCHORUS_REVISION"
# No NVIDIA banner entrypoint: CLI output stays clean.
ENTRYPOINT []
CMD ["aerochorus", "worker", "run"]

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
