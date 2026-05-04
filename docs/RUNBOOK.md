# One Service Runbook

This project is the single working Pashynska Photography booking service.

## Fast status check

```bash
cd /Users/andrzej/business/iryna/iryna-booking
pwd
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m pytest tests/test_booking_flow.py -q
launchctl list | grep -Ei 'pashynska|etransfer|iryna|booking' || true
lsof -nP -iTCP:5001 -sTCP:LISTEN || true
```

Expected:

- Tests pass.
- No old `com.pashynska.etransfer*` jobs are active.
- Port 5001 is either empty or owned by this project's Flask process.
- No old `/Users/andrzej/business/iryna/booking/app.py` or `/Users/andrzej/Iryna-Business` process is running.
- Telegram direct delivery to the configured `.env` `TELEGRAM_CHAT_ID` succeeds.

## Start local app

```bash
cd /Users/andrzej/business/iryna/iryna-booking
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 app.py
```

Open:

```text
http://127.0.0.1:5001/
```

## Start demo tunnel

In another terminal:

```bash
cd /Users/andrzej/business/iryna/iryna-booking
cloudflared tunnel --url http://127.0.0.1:5001
```

Copy the `https://...trycloudflare.com` URL from the log.

Update `.env` so Telegram messages and webhook point at the current public URL:

```bash
python3 - <<'PY'
from pathlib import Path
url = 'https://YOUR-CURRENT.trycloudflare.com'
p = Path('.env')
text = p.read_text(errors='ignore')
lines = []
seen = False
for line in text.splitlines():
    if line.startswith('BOOKING_BASE_URL='):
        lines.append(f'BOOKING_BASE_URL={url}')
        seen = True
    else:
        lines.append(line)
if not seen:
    lines.append(f'BOOKING_BASE_URL={url}')
p.write_text('\n'.join(lines).rstrip() + '\n')
PY
```

Set Telegram webhook for inline buttons:

```bash
python3 - <<'PY'
import os, requests
from dotenv import load_dotenv
load_dotenv('.env')
base = os.environ['BOOKING_BASE_URL'].rstrip('/')
token = os.environ['TELEGRAM_BOT_TOKEN']
r = requests.post(f'https://api.telegram.org/bot{token}/setWebhook', json={
    'url': f'{base}/telegram/webhook',
    'allowed_updates': ['callback_query'],
}, timeout=15)
print(r.json().get('ok'))
PY
```

## Full manual test

1. Open the public URL.
2. Choose an available slot.
3. Fill name/email/phone/Instagram.
4. Click Continue to Payment.
5. Confirm URL includes `booking_id=...`.
6. Click “I've Sent the Payment”.
7. Confirm success page opens.
8. Confirm Telegram admin chat receives a booking/payment message with:
   - client name/email/phone/Instagram
   - date and time slot
   - expected deposit
   - buttons: `✅ Confirm paid`, `❌ Cancel / release`, `👀 Open admin`
9. Re-open public URL in private/incognito mode.
10. Confirm the selected slot is no longer available.
11. Check SQLite:

```bash
sqlite3 bookings.db "select id,date,time,name,email,status,confirmed,paid,reserved_until from bookings order by id desc limit 5;"
```

12. If testing with a synthetic booking, cancel it afterwards so the slot is free again:

```bash
python3 - <<'PY'
import app
app.cancel_booking_release(BOOKING_ID_HERE)
PY
```

The cancellation/release path must free the slot for future reservations. Regression test: `test_cancelled_slot_can_be_reserved_again`.

Expected new row status immediately after fake/no real payment:

```text
pending_payment | confirmed=0 | paid=0
```

If a real matching Interac e-Transfer email arrives, `timed_cron.py` should later update it to:

```text
confirmed | confirmed=1 | paid=1
```

## Stop services

If started manually:

```bash
pkill -f '/Users/andrzej/business/iryna/iryna-booking/app.py' || true
pkill -f 'cloudflared tunnel --url http://127.0.0.1:5001' || true
```

## Legacy services

Do not use these as the active service:

- `/Users/andrzej/business/iryna/booking`
- `/Users/andrzej/Iryna-Business`

Old plists were disabled and moved under:

```text
/Users/andrzej/business/iryna/iryna-booking/disabled_launchagents/
```
