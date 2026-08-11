# Multi-stage Dockerfile for LiveKit Dashboard

# Stage 1: Build stage
FROM python:3.11-slim AS builder

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    curl && \
    rm -rf /var/lib/apt/lists/*

# Install Poetry using pip (more reliable for Docker)
RUN pip install poetry

# Copy dependency files
COPY pyproject.toml poetry.lock* ./

# Configure Poetry and install dependencies
RUN poetry config virtualenvs.create false && \
    poetry install --without dev --no-interaction --no-ansi

# Stage 2: Runtime stage
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies only
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    curl && \
    rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY app ./app

# Create non-root user
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

USER appuser

# The listen port is taken from $PORT at runtime so the image works unchanged
# behind a PaaS proxy (Coolify, Railway, Fly) that picks the port for you.
# Keep this in sync with the platform's "exposed port" setting — a mismatch
# gives a healthy container that the proxy cannot reach ("bad gateway").
ENV PORT=8000
EXPOSE 8000

# Probes the same port the app listens on, so it cannot report healthy while
# the proxy talks to a dead port.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# Shell form so $PORT expands; `exec` hands PID 1 to uvicorn so SIGTERM reaches
# it directly and the container stops in ~1s instead of being SIGKILLed after
# Docker's 10s grace period.
# --proxy-headers/--forwarded-allow-ips: the app runs behind a reverse proxy,
# so client IPs and the request scheme come from X-Forwarded-*.
CMD exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --proxy-headers \
    --forwarded-allow-ips='*'

