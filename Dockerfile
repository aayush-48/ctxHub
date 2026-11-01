# One image serves the REST API, the MCP endpoint, the login pages and the web viewer.

# --- web viewer -------------------------------------------------------------
FROM node:20-alpine AS web
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
# vite writes to ../server/src/ctx_server/static/app
RUN mkdir -p /build/server/src/ctx_server && npm run build

# --- python app -------------------------------------------------------------
FROM python:3.12-slim AS app
COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/srv/.venv \
    PATH="/srv/.venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /srv

# Set WITH_NLI=true to bundle the NLI cross-encoder prefilter (adds PyTorch, ~1 GB).
ARG WITH_NLI=false

# Dependencies first, for layer caching.
COPY pyproject.toml uv.lock ./
COPY server/pyproject.toml server/pyproject.toml
COPY cli/pyproject.toml cli/pyproject.toml
RUN uv sync --frozen --no-dev --package ctx-server --no-install-workspace \
      $( [ "$WITH_NLI" = "true" ] && echo "--extra nli" )

COPY server/ server/
COPY cli/ cli/
COPY --from=web /build/server/src/ctx_server/static/app server/src/ctx_server/static/app
RUN uv sync --frozen --no-dev --package ctx-server \
      $( [ "$WITH_NLI" = "true" ] && echo "--extra nli" )

RUN useradd --system --uid 10001 ctx && mkdir -p /home/ctx && chown ctx /home/ctx
USER ctx
ENV HOME=/home/ctx CTX_ENV=prod PORT=8080
EXPOSE 8080
# Cloud Run sets PORT and terminates TLS in front of the container.
CMD ["sh", "-c", "exec uvicorn ctx_server.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --timeout-graceful-shutdown 20"]
