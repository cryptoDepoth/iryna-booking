#!/bin/zsh
set -euo pipefail

APP="iryna-booking"
FLYCTL="/Users/andrzej/.fly/bin/flyctl"
TOKEN_FILE="/Users/andrzej/.fly/token.txt"
# LaunchAgents do not inherit the interactive app's macOS iCloud permission.
# Keep the automated copy in a private local folder (a separate failure domain
# from Fly); verified bundles can additionally be copied to iCloud manually.
DEST_DIR="/Users/andrzej/Pashynska-Booking-Backups"

[[ -x "$FLYCTL" ]] || { print -u2 "flyctl is unavailable"; exit 1; }
[[ -s "$TOKEN_FILE" ]] || { print -u2 "Fly access token is unavailable"; exit 1; }
mkdir -p "$DEST_DIR"
export FLY_API_TOKEN="$(tr -d '\n' < "$TOKEN_FILE")"

remote="$($FLYCTL ssh console -q -a "$APP" -C "sh -lc 'ls -1t /data/backups/pashynska_backup_*.zip 2>/dev/null | head -1'")"
remote="${remote##*$'\n'}"
[[ "$remote" == /data/backups/pashynska_backup_*.zip ]] || {
  print -u2 "No portable production backup is available yet"
  exit 1
}

filename="${remote:t}"
target="$DEST_DIR/$filename"
if [[ ! -f "$target" ]]; then
  temp="$target.partial"
  rm -f "$temp"
  "$FLYCTL" ssh sftp get -q -a "$APP" "$remote" "$temp"
  unzip -tq "$temp" >/dev/null
  mv "$temp" "$target"
  chmod 600 "$target"
fi

# Ninety days off-site is long enough to recover accidental deletions while
# keeping the photographer's client data footprint bounded.
find "$DEST_DIR" -type f -name 'pashynska_backup_*.zip' -mtime +90 -delete
print "Verified off-site backup: $target"
