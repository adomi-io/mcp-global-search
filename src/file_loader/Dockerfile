FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCS_DIR=/volumes/input \
    MEILISEARCH_HOST=http://meilisearch:7700 \
    MEILISEARCH_BATCH_SIZE=200 \
    MEILISEARCH_MAX_BYTES=2097152 \
    MEILISEARCH_ALLOWED_EXTS=.md,.mdx,.txt,.json,.yml,.yaml,.toml,.js,.ts,.vue,.css,.html,.sh,.py,.csv \
    LOG_LEVEL=INFO \
    MEILI_EMBEDDER_NAME=openai \
    OPENAI_EMBED_MODEL=text-embedding-3-small \
    CHUNK_SIZE=1200 \
    CHUNK_OVERLAP=150

RUN set -eux; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    pip install --no-cache-dir \
      watchdog \
      meilisearch \
      langchain-community \
      langchain-text-splitters

WORKDIR /app
COPY loader.py /app/loader.py

CMD ["python", "/app/loader.py"]
