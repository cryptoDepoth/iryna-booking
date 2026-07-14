#!/bin/bash
# Auto-fix corrupted events.yaml before app starts
if [ -f /data/events.yaml ]; then
    python3 -c "
import yaml, sys
try:
    with open('/data/events.yaml') as f:
        data = yaml.safe_load(f)
    print('[start] events.yaml OK')
except yaml.scanner.ScannerError as e:
    line = int(str(e).split('line ')[1].split(',')[0])
    print(f'[fix] Truncating events.yaml at line {line}')
    with open('/data/events.yaml') as f:
        lines = f.readlines()
    with open('/data/events.yaml', 'w') as f:
        f.writelines(lines[:line-1])
    print(f'[fix] Removed {len(lines)-line+1} corrupted lines')
" 2>&1
fi
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

# Gmail exposes the Sent folder with a localized IMAP name in this mailbox.
# Avoid failing confirmations with: "cannot add IMAP message / Folder doesn't exist".
folder.aliases.inbox = "INBOX"
folder.aliases.sent = "[Gmail]/Отправленные"
message.send.save-copy = false

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
export HIMALAYA_CONFIG="/data/.config/himalaya/config.toml"
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
export ASSISTANT_KNOWLEDGE_PATH="${ASSISTANT_KNOWLEDGE_PATH:-/data/assistant_knowledge.jsonl}"
# Calendar credentials are provided through Fly secrets; point the app at the
# bundled CLI wrapper so confirmed bookings can actually create events.
export GCAL_HELPER="${GCAL_HELPER:-/app/gcal_helper.py}"
# PHOTOS_DIR keeps admin-uploaded photos on the persistent volume so they
# survive container restarts and redeploys (the bundled /app/static/images
# directory is wiped on every deploy).
export PHOTOS_DIR=/data/images
mkdir -p "$PHOTOS_DIR"

echo "[start] DB_PATH=$DB_PATH"
echo "[start] EVENTS_YAML_PATH=$EVENTS_YAML_PATH"
echo "[start] PHOTOS_DIR=$PHOTOS_DIR"
echo "[start] ASSISTANT_KNOWLEDGE_PATH=$ASSISTANT_KNOWLEDGE_PATH"
echo "[start] GCAL_HELPER=$GCAL_HELPER"

# ── Launch Gunicorn ────────────────────────────────────────────────────────────
# 1 worker × 4 threads keeps memory under 256MB while letting slow Notion /
# Calendar / SMTP calls run in parallel — without this the entire site freezes
# for ~20 seconds every time admin clicks "Confirm". The global watcher and
# email scheduler threads live in this same process, so a single worker is
# safer than multiple (no double-firing of cron jobs).
exec gunicorn --bind :8080 --worker-class gthread --workers 1 --threads 4 --timeout 120 app:app
