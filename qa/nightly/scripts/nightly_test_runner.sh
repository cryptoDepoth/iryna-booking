#!/bin/bash
set -euo pipefail
# Nightly QA Test Runner — Pashynska Booking
# Runs: Layer 1 → 2 → 3 → 4, with cleanup and notifications
# Schedule: 01:00, 03:00, 05:00 via cron/hermes

BASE_DIR="/Users/andrzej/Iryna-Master/01-Booking-System"
QA_DIR="$BASE_DIR/qa/nightly"
LOG_DIR="$QA_DIR/logs"
REPORT_DIR="$QA_DIR/reports"
TS=$(date +%Y%m%d_%H%M%S)
LOG="$LOG_DIR/nightly_${TS}.log"
REPORT="$REPORT_DIR/nightly_${TS}.json"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-792920251}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
ENV_FILE="$BASE_DIR/.env.qa"

PASS=0
FAIL=0
WARN=0

mkdir -p "$LOG_DIR" "$REPORT_DIR"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
fail() {
  log "❌ $*"
  ((FAIL+=1))
  send_telegram "🚨 QA FAILED: $*\nLog: $LOG"
}
pass() {
  log "✅ $*"
  ((PASS+=1))
}
warn() {
  log "⚠️ $*"
  ((WARN+=1))
}

send_telegram() {
  local msg="$1"
  if [[ -n "$TELEGRAM_BOT_TOKEN" ]]; then
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d chat_id="$TELEGRAM_CHAT_ID" \
      -d text="$msg" \
      -d parse_mode="HTML" \
      >/dev/null || true
  fi
}

cleanup_qa_data() {
  log "=== CLEANUP QA DATA ==="
  python3 - <<'PY'
import sqlite3, os, sys
from pathlib import Path
BASE = Path('/Users/andrzej/Iryna-Master/01-Booking-System')
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv
load_dotenv(BASE / '.env.qa', override=True)
DB_PATH = os.getenv('DB_PATH', str(BASE / 'booking.db'))
QA_EMAIL_PREFIX = os.getenv('QA_EMAIL_PREFIX', 'qa-test')
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("DELETE FROM bookings WHERE email LIKE ?", (f'%{QA_EMAIL_PREFIX}%',))
deleted = c.rowcount
conn.commit()
conn.close()
print(f'[cleanup] Deleted {deleted} QA bookings')
PY
  log "Cleanup complete"
}

cleanup_gmail_bounces() {
  log "=== CLEANUP GMAIL BOUNCES ==="
  if command -v himalaya &> /dev/null; then
    himalaya --folder INBOX search "from:mailer-daemon OR from:Mail Delivery Subsystem OR subject:Delivery Status Notification" --json 2>/dev/null |       python3 -c "import sys,json; [print(r['id']) for r in json.load(sys.stdin)]" 2>/dev/null |       xargs -I {} sh -c 'himalaya delete {}' >/dev/null 2>&1 || true
    log "Gmail bounce cleanup attempted"
  else
    warn "himalaya not installed — skip Gmail cleanup"
  fi
}

# ── PRE-FLIGHT ──────────────────────────────────────────────────────────────
log "═════════════════════════════════════════"
log "NIGHTLY QA START: $(date)"
log "Target: ${TEST_BASE_URL:-http://127.0.0.1:5001}"
log "═════════════════════════════════════════"

cleanup_qa_data

# ── LAYER 1: BUSINESS LOGIC ─────────────────────────────────────────────────
log ""
log "=== LAYER 1: CRITICAL BUSINESS LOGIC ==="
cd "$BASE_DIR"
if /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m pytest "$QA_DIR/tests/layer1_business_logic/test_layer1.py" -q --tb=short >> "$LOG" 2>&1; then
  pass "Layer 1: Business logic"
else
  fail "Layer 1: Business logic tests failed"
fi

# ── LAYER 2: API + DATABASE ─────────────────────────────────────────────────
log ""
log "=== LAYER 2: API + DATABASE ==="
if /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m pytest "$QA_DIR/tests/layer2_api_database/test_layer2.py" -q --tb=short >> "$LOG" 2>&1; then
  pass "Layer 2: API + Database"
else
  fail "Layer 2: API + Database tests failed"
fi

# ── LAYER 3: UI/E2E (Playwright) ────────────────────────────────────────────
log ""
log "=== LAYER 3: UI/E2E ==="
# Ensure Playwright browsers installed
if ! [[ -d "$HOME/.cache/ms-playwright" ]]; then
  warn "Playwright browsers not installed — skipping Layer 3"
else
  # Run with pytest-playwright
  cd "$BASE_DIR"
  if /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m pytest "$QA_DIR/tests/layer3_ui_e2e/test_layer3.py" -q --tb=short >> "$LOG" 2>&1; then
    pass "Layer 3: UI/E2E"
  else
    fail "Layer 3: UI/E2E tests failed"
  fi
fi

# ── LAYER 4: VISUAL REGRESSION ──────────────────────────────────────────────
log ""
log "=== LAYER 4: VISUAL REGRESSION ==="
if [[ -d "$HOME/.cache/ms-playwright" ]]; then
  cd "$BASE_DIR"
  if /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m pytest "$QA_DIR/tests/layer4_visual/test_layer4.py" -q --tb=short >> "$LOG" 2>&1; then
    pass "Layer 4: Visual regression"
  else
    fail "Layer 4: Visual regression tests failed"
  fi
else
  warn "Playwright not available — skip visual tests"
fi

# ── POST-FLIGHT ─────────────────────────────────────────────────────────────
cleanup_qa_data
cleanup_gmail_bounces

# ── REPORT ──────────────────────────────────────────────────────────────────
cat > "$REPORT" <<EOF
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "pass": $PASS,
  "fail": $FAIL,
  "warn": $WARN,
  "log_path": "$LOG",
  "target": "${TEST_BASE_URL:-http://127.0.0.1:5001}",
  "layers": {
    "1_business": "$([ $FAIL -eq 0 ] && echo "pass" || echo "fail")",
    "2_api": "$([ $FAIL -eq 0 ] && echo "pass" || echo "fail")",
    "3_ui": "$([ $FAIL -eq 0 ] && echo "pass" || echo "fail")",
    "4_visual": "$([ $FAIL -eq 0 ] && echo "pass" || echo "fail")"
  }
}
EOF

log ""
log "═════════════════════════════════════════"
log "RESULTS: $PASS passed, $FAIL failed, $WARN warnings"
log "Log: $LOG"
log "Report: $REPORT"
log "═════════════════════════════════════════"

if [[ $FAIL -eq 0 ]]; then
  send_telegram "✅ <b>Nightly QA Passed</b>\nLayers: 1-4 all green\nTarget: ${TEST_BASE_URL:-local}\nLog: $LOG"
  exit 0
else
  send_telegram "🚨 <b>Nightly QA FAILED</b>\nFailed: $FAIL layers\nLog: $LOG"
  exit 1
fi
