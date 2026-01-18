FROM python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCS_ROOT=/volumes/output \
    DOWNLOAD_CONFIG=/config/download.yml \
    PORT=8080

RUN set -eux; \
    apk add --no-cache git ca-certificates bash curl; \
    pip install --no-cache-dir Flask PyYAML requests gunicorn

WORKDIR /app
COPY app.py /app/app.py

EXPOSE 8080
# Use a single Gunicorn worker to avoid concurrent clones into the same staging dir
# The app exposes a factory `build_app()`.
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8080", "--access-logfile", "-", "app:build_app()"]
