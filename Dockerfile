# Container image for remote HTTP deployments (Cloud Run, Docker).
# Local stdio use does not need this; see README.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Dependencies first for layer caching, then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# Cloud Run sends traffic to $PORT (8080 by default) on all interfaces.
ENV MTG_MCP_TRANSPORT=http \
    MTG_MCP_HTTP_HOST=0.0.0.0 \
    MTG_MCP_HTTP_PORT=8080 \
    FASTMCP_STATELESS_HTTP=true

# Runs as root: Cloud Run's sandbox isolates the container, and the Cloud Storage
# volume at /data (OAuth state) is writable by root without extra mount options.
EXPOSE 8080
CMD ["/app/.venv/bin/mtg-mcp-server", "http"]
