# flatwatch — single-container alerting service.
# Multi-arch: builds for linux/amd64 and linux/arm64 (Synology NAS).
FROM python:3.12-slim

# Build tooling needed by feedparser's sgmllib3k sdist, removed after install.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential

COPY app/ ./app/

# Image provenance, surfaced in every run record's `version` field.
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# Persisted dedup store, heartbeat, and run-log backlog live here.
VOLUME ["/data"]

# Optional health endpoint (only active if HEALTHCHECK_PORT is set).
EXPOSE 8080
HEALTHCHECK --interval=5m --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS "http://localhost:${HEALTHCHECK_PORT:-8080}/health" || exit 1

ENTRYPOINT ["python", "-m", "app.main"]
