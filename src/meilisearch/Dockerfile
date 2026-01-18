FROM getmeili/meilisearch:latest

ENV MEILI_HTTP_ADDR=0.0.0.0:7700 \
    MEILI_DB_PATH=/meili_data/db \
    MEILI_NO_ANALYTICS=true \
    MEILI_ENV=production

RUN set -eux; \
      apk add --no-cache curl ca-certificates;

# Ensure the internal data directory is writable by any UID (we run the container
# with a host-provided UID/GID via docker-compose).
RUN set -eux; \
    mkdir -p /meili_data/db; \
    chmod -R 0777 /meili_data

COPY entrypoint.sh /entrypoint.sh

RUN set -eux; sed -i 's/\r$//' /entrypoint.sh; chmod +x /entrypoint.sh

VOLUME ["/volumes/sources", "/meili_data"]

EXPOSE 7700

ENTRYPOINT ["/entrypoint.sh"]
