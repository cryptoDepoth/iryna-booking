#!/bin/bash
set -e

# ── Ensure persistent data directory exists ────────────────────────────────────
mkdir -p /data /data/backups

# ── Copy bundled events.yaml to /data on FIRST RUN only ───────────────────────
if [ ! -f /data/events.yaml ]; then
    echo "[start] First run — copying bundled events.yaml to /data/events.yaml"
    cp /app/events.yaml /data/events.yaml
else
    echo "[start] Using existing /data/events.yaml"
fi

# ── Generate Himalaya config (Linux server paths, env-based password) ──────────
HIMALAYA_DIR="/data/.config/himalaya"
mkdir -p "$HIMALAYA_DIR"

# Write password to file (Himalaya auth.cmd reads from file)
if [ -n "$GMAIL_APP_PASSWORD" ]; then
    echo "$GMAIL_APP_PASSWORD" > "$HIMALAYA_DIR/iryna_gmail_app_password"
    chmod 600 "$HIMALAYA_DIR/iryna_gmail_app_password"
    echo "[start] Gmail app password configured"
else
    echo "[WARN] GMAIL_APP_PASSWORD not set — e-Transfer auto-check will fail"
fi

# Write Himalaya config.toml
cat > "$HIMALAYA_DIR/config.toml" <<'EOF'
[accounts.iryna]
default = true
email = "iryna.pashynska@gmail.com"
display-name = "Pashynska Photography"

backend.type = "imap"
backend.host = "imap.gmail.com"
backend.port = 993
backend.encryption.type = "tls"
backend.login = "iryna.pashynska@gmail.com"
backend.auth.type = "password"
backend.auth.cmd = "cat /data/.config/himalaya/iryna_gmail_app_password"

message.send.backend.type = "smtp"
message.send.backend.host = "smtp.gmail.com"
message.send.backend.port = 587
message.send.backend.encryption.type = "start-tls"
message.send.backend.login = "iryna.pashynska@gmail.com"
message.send.backend.auth.type = "password"
message.send.backend.auth.cmd = "cat /data/.config/himalaya/iryna_gmail_app_password"
EOF

# Set XDG_CONFIG_HOME so Himalaya finds the config
export XDG_CONFIG_HOME="/data/.config"
echo "[start] Himalaya config: $HIMALAYA_DIR/config.toml"

# ── Generate a stable Flask secret key if not provided ────────────────────────
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
