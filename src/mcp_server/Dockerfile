FROM python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEILISEARCH_HOST=http://meilisearch:7700

RUN set -eux; \
    apk add --no-cache ca-certificates bash curl; \
    pip install --no-cache-dir fastmcp httpx meilisearch-python-sdk

WORKDIR /app
COPY meili_mcp.py /app/meili_mcp.py

# Default to HTTP transport for docker-compose; configurable via MCP_TRANSPORT.
ENV MCP_TRANSPORT=http
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000
ENTRYPOINT ["python", "/app/meili_mcp.py"]
