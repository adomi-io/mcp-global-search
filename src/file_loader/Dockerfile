FROM python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCS_DIR=/volumes/input \
    MEILISEARCH_HOST=http://meilisearch:7700

RUN set -eux; \
    apk add --no-cache ca-certificates bash curl; \
    pip install --no-cache-dir requests watchdog PyYAML

WORKDIR /app
COPY loader.py /app/loader.py

CMD ["python", "/app/loader.py"]
