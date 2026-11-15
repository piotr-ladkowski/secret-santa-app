# syntax=docker/dockerfile:1.7
#
#   docker build -t secret-santa .
#   docker run --rm -p 8000:8000 -e ADMIN_PASSWORD=changeme123 -e SECRET_KEY=$(openssl rand -hex 32) secret-santa
ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.11.21

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# --- dependencies + project, into a relocatable venv -------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --- runtime ----------------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/piotr-ladkowski/secret-santa-app" \
      org.opencontainers.image.description="Secret Santa sign-up and draw" \
      org.opencontainers.image.licenses="UNLICENSED"

RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir /data && chown 10001:10001 /data

COPY --from=build /app/.venv /app/.venv

# OTEL_*: tracing is off unless the deployment sets OTEL_TRACES_EXPORTER=otlp and an endpoint.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_URL=sqlite:////data/secret-santa.db \
    OTEL_SERVICE_NAME=secret-santa \
    OTEL_TRACES_EXPORTER=none \
    OTEL_METRICS_EXPORTER=none \
    OTEL_LOGS_EXPORTER=none

# Numeric on purpose: Kubernetes' runAsNonRoot needs to see a non-zero uid.
USER 10001:10001
WORKDIR /data
VOLUME /data
EXPOSE 8000
# One worker: SQLite, one small group of users. --proxy-headers so logs show the real client.
CMD ["opentelemetry-instrument", "uvicorn", "secret_santa.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--no-server-header", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
