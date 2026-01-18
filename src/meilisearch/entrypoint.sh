#!/bin/sh
set -eu

: "${MEILI_HTTP_ADDR:=0.0.0.0:7700}"
: "${MEILI_DB_PATH:=/meili_data/db}"

KEY_FILE="/volumes/meilisearch/master_key"

# If the mount provided a directory at the KEY_FILE path, resolve to a file inside it
if [ -d "$KEY_FILE" ]; then
  KEY_FILE="$KEY_FILE/master_key"
fi

mkdir -p "$(dirname "$KEY_FILE")" "$MEILI_DB_PATH"

if [ -n "${MEILI_MASTER_KEY:-}" ]; then
  :
elif [ -f "$KEY_FILE" ]; then
  MEILI_MASTER_KEY="$(cat "$KEY_FILE")"
  export MEILI_MASTER_KEY
else
  MEILI_MASTER_KEY="$(/bin/sh -c 'python3 - <<PY\nimport secrets;print(secrets.token_urlsafe(48))\nPY' 2>/dev/null || /bin/sh -c 'head -c 32 /dev/urandom | base64')"
  export MEILI_MASTER_KEY

  umask 077

  printf "%s" "$MEILI_MASTER_KEY" > "$KEY_FILE"

  chmod 600 "$KEY_FILE"
fi

exec /bin/meilisearch \
  --http-addr "$MEILI_HTTP_ADDR" \
  --db-path "$MEILI_DB_PATH" \
  --master-key "$MEILI_MASTER_KEY"
