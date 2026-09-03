#!/usr/bin/env sh
set -eu

MANAGE_PY="${LITEBOOKS_MANAGE_PY:-manage.py}"

if [ ! -f "${MANAGE_PY}" ]; then
  echo "Missing ${MANAGE_PY}. Add the LiteBooks Django application source before starting the container." >&2
  exit 1
fi

wait_for_database() {
  python - <<'PY'
import os
import socket
import sys
import time
from urllib.parse import urlparse

database_url = os.environ.get("DATABASE_URL", "")
parsed = urlparse(database_url)

if not parsed.hostname:
    sys.exit(0)

host = parsed.hostname
port = parsed.port or 5432
deadline = time.time() + int(os.environ.get("LITEBOOKS_DB_WAIT_TIMEOUT", "60"))

while True:
    try:
        with socket.create_connection((host, port), timeout=2):
            sys.exit(0)
    except OSError:
        if time.time() >= deadline:
            print(f"Timed out waiting for database at {host}:{port}", file=sys.stderr)
            sys.exit(1)
        time.sleep(2)
PY
}

if [ "${LITEBOOKS_WAIT_FOR_DB:-true}" = "true" ]; then
  echo "Waiting for database..."
  wait_for_database
fi

if [ "${LITEBOOKS_AUTO_MIGRATE:-true}" = "true" ]; then
  echo "Running database migrations..."
  python "${MANAGE_PY}" migrate --noinput
fi

if [ "${LITEBOOKS_COLLECT_STATIC:-false}" = "true" ]; then
  echo "Collecting static files..."
  python "${MANAGE_PY}" collectstatic --noinput || echo "Static collection skipped or not configured." >&2
fi

exec "$@"
