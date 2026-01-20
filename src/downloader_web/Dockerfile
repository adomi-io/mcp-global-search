FROM python:3.11-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCS_ROOT=/volumes/output \
    STATE_ROOT=/volumes/state \
    CONFIG_FILE=/config/download.yml \
    PORT=8080 \
    MAX_WORKERS=4 \
    HTTP_TIMEOUT_SECS=30

RUN set -eux; \
    apk add --no-cache \
      git \
      ca-certificates \
      bash \
      curl \
      rsync; \
    pip install --no-cache-dir Flask PyYAML requests gunicorn

WORKDIR /app
COPY app.py /app/app.py

EXPOSE 8080

CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:8080", "--access-logfile", "-", "app:build_app()"]
