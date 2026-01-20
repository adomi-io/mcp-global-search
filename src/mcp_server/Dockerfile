FROM python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEILISEARCH_HOST=http://meilisearch:7700 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

RUN set -eux; \
    apk add --no-cache ca-certificates bash curl; \
    pip install --no-cache-dir fastmcp httpx starlette pydantic

WORKDIR /app

COPY meili_mcp.py /app/meili_mcp.py

ENTRYPOINT ["python", "/app/meili_mcp.py"]
