#!/usr/bin/env bash
# One-time setup for permanent booking system (Flask + named Cloudflare tunnel)
# Run from ~/business/iryna/booking
set -e
cd "$(dirname "$0")"

echo "==> 1. Authenticate cloudflared (opens browser)"
cloudflared tunnel login

echo "==> 2. Create named tunnel"
cloudflared tunnel create iryna-booking

TUNNEL_ID=$(cloudflared tunnel list | awk '/iryna-booking/{print $1}')
echo "Tunnel ID: $TUNNEL_ID"

echo "==> 3. Write tunnel config"
cat > ~/.cloudflared/config.yml <<EOF
tunnel: $TUNNEL_ID
credentials-file: $HOME/.cloudflared/$TUNNEL_ID.json
ingress:
  - hostname: $TUNNEL_ID.cfargotunnel.com
    service: http://localhost:5001
  - service: http_status:404
EOF

echo "==> 4. Public URL: https://$TUNNEL_ID.cfargotunnel.com"
echo "    (If you have a domain, run:  cloudflared tunnel route dns iryna-booking booking.yourdomain.com)"

echo "==> 5. Install LaunchAgents (auto-start Flask + tunnel on login)"
mkdir -p ~/Library/LaunchAgents

cat > ~/Library/LaunchAgents/com.iryna.booking.flask.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.iryna.booking.flask</string>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string><string>python3</string><string>app.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>NOTION_API_KEY</key><string>${NOTION_API_KEY:-PUT-YOUR-KEY-HERE}</string>
    <key>NOTION_DATABASE_ID</key><string>d722613f-a8b5-438f-bcf0-0ef9f84c3d78</string>
    <key>GCAL_HELPER</key><string>$PWD/gcal_helper.py</string>
    <key>FLASK_DEBUG</key><string>0</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$PWD/logs/flask.log</string>
  <key>StandardErrorPath</key><string>$PWD/logs/flask.err</string>
</dict></plist>
EOF

cat > ~/Library/LaunchAgents/com.iryna.booking.tunnel.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.iryna.booking.tunnel</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(which cloudflared)</string><string>tunnel</string><string>run</string><string>iryna-booking</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$PWD/logs/tunnel.log</string>
  <key>StandardErrorPath</key><string>$PWD/logs/tunnel.err</string>
</dict></plist>
EOF

launchctl unload ~/Library/LaunchAgents/com.iryna.booking.flask.plist 2>/dev/null || true
launchctl unload ~/Library/LaunchAgents/com.iryna.booking.tunnel.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/com.iryna.booking.flask.plist
launchctl load ~/Library/LaunchAgents/com.iryna.booking.tunnel.plist

echo ""
echo "✅ Done. System will now auto-start on login."
echo "   Flask:  http://localhost:5001"
echo "   Public: https://$TUNNEL_ID.cfargotunnel.com"
echo ""
echo "To stop:    launchctl unload ~/Library/LaunchAgents/com.iryna.booking.*.plist"
echo "To restart: launchctl kickstart -k gui/\$UID/com.iryna.booking.flask"
