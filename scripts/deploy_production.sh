#!/usr/bin/env bash
set -euo pipefail

EXPECTED_DIR="/Users/andrzej/Iryna-Master/01-Booking-System"
APP="iryna-booking"
FLY="/Users/andrzej/.fly/bin/flyctl"

if [[ "$(pwd)" != "$EXPECTED_DIR" ]]; then
  echo "❌ Wrong deploy directory: $(pwd)" >&2
  echo "✅ Deploy only from: $EXPECTED_DIR" >&2
  exit 2
fi

if [[ ! -f "DEPLOY_SOURCE_OF_TRUTH.md" ]]; then
  echo "❌ Missing DEPLOY_SOURCE_OF_TRUTH.md — refusing to deploy." >&2
  exit 2
fi

if [[ ! -x "$FLY" ]]; then
  echo "❌ flyctl not found at $FLY" >&2
  exit 2
fi

python3 -m pytest -q
"$FLY" deploy --remote-only --yes -a "$APP"

python - <<'PY'
import urllib.request, re, sys
checks = [
    ('https://book.pashynskaphoto.com/healthz', 'ok'),
    ('https://book.pashynskaphoto.com/events', None),
    ('https://book.pashynskaphoto.com/', 'Calgary Family, Maternity & Wedding Photographer'),
]
for url, marker in checks:
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'}), timeout=25) as r:
        body = r.read().decode('utf-8', 'replace')
        print(f'{url} -> {r.status} {r.geturl()} len={len(body)}')
        if r.status != 200:
            sys.exit(f'Bad status for {url}: {r.status}')
        if marker and marker not in body:
            sys.exit(f'Missing marker {marker!r} in {url}')
print('✅ Production smoke checks passed')
PY
