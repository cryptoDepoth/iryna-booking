#!/bin/bash
set -e

# ── Ensure persistent data directory exists ────────────────────────────────────
mkdir -p /data /data/backups

# ── Copy bundled events.yaml to /data on FIRST RUN only ───────────────────────
# This preserves any events the photographer creates via the admin panel across
# restarts and redeploys.  Only copies if the persistent copy doesn't exist yet.
if [ ! -f /data/events.yaml ]; then
    echo "[start] First run — copying bundled events.yaml to /data/events.yaml"
    cp /app/events.yaml /data/events.yaml
else
    echo "[start] Using existing /data/events.yaml"
fi

# ── Generate a stable Flask secret key if not provided ────────────────────────
# Storing it in /data means it survives restarts (same as the DB volume).
if [ -z "$FLASK_SECRET_KEY" ]; then
    if [ ! -f /data/.flask_secret ]; then
        python3 -c "import secrets; print(secrets.token_hex(32))" > /data/.flask_secret
        echo "[start] Generated new stable Flask secret key"
    fi
    export FLASK_SECRET_KEY="$(cat /data/.flask_secret)"
    echo "[start] Loaded Flask secret key from /data/.flask_secret"
fi

# ── Export persistent paths ────────────────────────────────────────────────────
export DB_PATH=/data/bookings.db
export BACKUP_DIR=/data/backups
export EVENTS_YAML_PATH=/data/events.yaml

echo "[start] DB_PATH=$DB_PATH"
echo "[start] EVENTS_YAML_PATH=$EVENTS_YAML_PATH"

# ── Launch Gunicorn ────────────────────────────────────────────────────────────
exec gunicorn --bind :8080 --workers 1 --timeout 120 app:app
