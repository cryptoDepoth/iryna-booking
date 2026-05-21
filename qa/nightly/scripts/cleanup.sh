#!/bin/bash
# Manual cleanup — deletes QA test data and Gmail bounces
BASE_DIR="/Users/andrzej/business/iryna/iryna-booking"
QA_DIR="$BASE_DIR/qa/nightly"

echo "=== CLEANUP QA DATA ==="
python3 - <<'PY'
import sqlite3, os, sys
from pathlib import Path
BASE = Path('/Users/andrzej/business/iryna/iryna-booking')
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

echo "=== CLEANUP GMAIL BOUNCES ==="
if command -v himalaya > /dev/null 2>&1; then
  himalaya --folder INBOX search "from:mailer-daemon OR from:Mail Delivery Subsystem" --json 2>/dev/null |     python3 -c "import sys,json; [print(r['id']) for r in json.load(sys.stdin)]" 2>/dev/null |     xargs -I {} sh -c 'himalaya delete {}' >/dev/null 2>1 || true
  echo "Gmail bounces cleaned"
else
  echo "himalaya not installed — skip"
fi

echo "=== CLEANUP SNAPSHOTS ==="
rm -f "$QA_DIR/snapshots/current"/*.png
echo "Done"
