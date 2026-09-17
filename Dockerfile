# Multi-target image:
#   api    — slim FastAPI (upload / download / content API / lightweight metadata extraction);
#            no heavy ML parsers (markitdown/onnx/pandas) or LibreOffice
#   worker — parse / convert / heavy deps (markitdown + LibreOffice + future tools)
#
# Preferred:
#   ./build.sh --native              # tags :{arch}-{ts}-{sha} + :local
#   # last_image_tag.txt → IMAGE_TAG / API_IMAGE / WORKER_IMAGE (source-able)
#
# Manual:
#   docker build --target api    -t agent-file-service-api:local .
#   docker build --target worker -t agent-file-service-worker:local .
#
# API and worker share a volume at /data (SQLite + local blobs + derivatives).

# ── Stage 1: API dependency build (core + metadata extractors) ───────────
FROM python:3.14-slim AS builder-api

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Lockfile-only install: do not copy or install the project itself.
# Runtime uses COPY'd /app/app.
COPY pyproject.toml uv.lock ./

RUN python -m venv --without-pip /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --frozen --no-dev --no-emit-project --extra metadata -o /tmp/requirements-api.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache -r /tmp/requirements-api.txt \
    && rm -f /tmp/requirements-api.txt

# ── Stage 2: Worker dependency build (adds parsers: markitdown/pandas/onnx)
FROM builder-api AS builder-worker

RUN --mount=type=cache,target=/root/.cache/uv \
    uv export --frozen --no-dev --no-emit-project --extra parsers -o /tmp/requirements-worker.txt \
    && uv pip install --python /opt/venv/bin/python --no-cache -r /tmp/requirements-worker.txt \
    && rm -f /tmp/requirements-worker.txt

# ── Stage 3: common runtime base (no heavy system packages) ──────────────
FROM python:3.14-slim AS runtime-base

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive

# curl: healthcheck (api)
RUN apt-get update -o Acquire::Retries=5 && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Clean up official Python image pip/ensurepip to avoid CVEs and save layer space
RUN python -m pip uninstall -y pip 2>/dev/null || true \
    && rm -rf /usr/local/lib/python*/ensurepip \
              /usr/local/lib/python*/site-packages/pip* \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app" \
    DEPLOY_MODE=lite \
    DATABASE_URL=sqlite:////data/db/file_service.db \
    STORAGE_BACKEND=local \
    LOCAL_STORAGE_PATH=/data \
    CONFIG_PATH=/app/config.yaml

COPY app ./app
COPY config.yaml ./config.yaml
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

# ── Stage 4: API image (lightweight) ─────────────────────────────────────
FROM runtime-base AS api

ENV SERVICE_ROLE=api

COPY --from=builder-api /opt/venv /opt/venv

# Dev console static assets only on api (worker does not serve HTTP UI).
COPY web ./web

# Fail the build if dev console static assets are missing (intranet offline deploy).
RUN test -f /app/web/dashboard.html \
    && test -f /app/web/auth.html \
    && test -f /app/web/shared/config.js \
    && test -f /app/web/shared/styles/app.css \
    && test -f /app/web/shared/js/chrome.js \
    && test -f /app/web/shared/js/uc.js \
    && test -f /app/web/shared/js/uc-strip.js \
    && test -d /app/web/shared/js/pages \
    && test "$(ls -1 /app/web/*.html 2>/dev/null | wc -l)" -ge 8

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ── Stage 5: Worker image (LibreOffice + markitdown + heavy deps) ─────────
FROM runtime-base AS worker

COPY --from=builder-worker /opt/venv /opt/venv

ENV DEBIAN_FRONTEND=noninteractive

# LibreOffice: legacy Office (doc/ppt/xls) → OOXML for layout
# fonts: CJK / Latin for conversion fidelity
# Future: ffmpeg, tesseract, etc. land here — not in api
RUN apt-get update && apt-get install -y --no-install-recommends \
    default-jre-headless \
    fonts-dejavu-core \
    fonts-wqy-zenhei \
    libreoffice-calc \
    libreoffice-impress \
    libreoffice-java-common \
    libreoffice-writer \
    && rm -rf /var/lib/apt/lists/* \
    && (command -v soffice >/dev/null 2>&1 || command -v libreoffice >/dev/null 2>&1)

ENV SERVICE_ROLE=worker \
    SAL_USE_VCLPLUGIN=svp \
    LEGACY_OFFICE_ENABLED=true

# concurrency=1 is safer with shared SQLite (lite); scale up under Postgres (full)
CMD ["celery", "-A", "app.tasks.celery_app.celery_app", "worker", \
     "--loglevel=info", "--concurrency=1"]
